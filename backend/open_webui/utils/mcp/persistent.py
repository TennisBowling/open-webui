"""Long-lived stdio MCP processes owned by a single asyncio task.

AnyIO's stdio transport requires teardown in the task that opened it.  Each
entry below is therefore an actor: callers submit MCP operations through a
queue while the actor alone owns the MCPClient and its child process.
Individual protocol requests run concurrently; the owner drains them before
closing the transport, so a long IDA operation does not block another chat.
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from functools import partial
from typing import Iterable, Optional

from open_webui.utils.mcp.client import MCPClient

log = logging.getLogger(__name__)

# How often an idle actor checks that its transport is still alive.  The SDK's
# receive loop ends silently when a stdio child exits (stdout EOF); without this
# poll the actor would sit on ``queue.get()`` forever, looking healthy.
LIVENESS_POLL_SECONDS = float(os.environ.get("PERSISTENT_MCP_LIVENESS_POLL", "5"))
# Supervisor tick and per-key restart backoff (doubling, capped).
SUPERVISE_INTERVAL_SECONDS = float(os.environ.get("PERSISTENT_MCP_SUPERVISE_INTERVAL", "5"))
RESTART_BACKOFF_BASE_SECONDS = float(os.environ.get("PERSISTENT_MCP_BACKOFF_BASE", "5"))
RESTART_BACKOFF_MAX_SECONDS = float(os.environ.get("PERSISTENT_MCP_BACKOFF_MAX", "300"))

_MISSING = object()


class TransportClosed(ConnectionError):
    """The child process/transport behind a persistent session went away."""


def _transport_alive(client) -> bool:
    """False once the MCP session's receive loop has ended (stream closed).

    Clients without a ``session`` attribute (test fakes) are always alive; an
    unknown stream shape is treated as alive rather than killing a healthy
    process over an SDK refactor.
    """

    session = getattr(client, "session", _MISSING)
    if session is _MISSING:
        return True
    if session is None:
        return False
    stream = getattr(session, "_read_stream", None)
    # Unwrap our _EventFilter to the SDK's MemoryObjectReceiveStream.
    stream = getattr(stream, "_stream", stream)
    return getattr(stream, "_closed", False) is not True


@dataclass
class _Request:
    method: str
    args: tuple
    kwargs: dict
    future: asyncio.Future


class PersistentMCPClient:
    """Stable handle for one logical persistent MCP connection.

    A handle deliberately resolves the current process through the manager for
    every operation.  Chat turns retain these handles for a long time, while a
    settings save can restart the underlying stdio process at any moment.  If a
    handle pointed at an individual ``_Entry``, every turn that started before
    the restart would keep calling the retired process.
    """

    def __init__(self, manager: "PersistentMCPManager", key: str):
        self._manager = manager
        self._key = key

    async def list_tool_specs(self):
        return await self._manager.request(self._key, "list_tool_specs")

    async def chat_activity(self, meta):
        return await self._manager.request(self._key, "chat_activity", meta)

    async def call_tool(self, function_name, function_args, **kwargs):
        return await self._manager.request(
            self._key,
            "call_tool", function_name, function_args, **kwargs
        )

    async def list_resources(self, cursor=None):
        return await self._manager.request(self._key, "list_resources", cursor)

    async def read_resource(self, uri):
        return await self._manager.request(self._key, "read_resource", uri)

    async def disconnect(self):
        # A borrowed persistent client is intentionally not turn-scoped.
        return None


class _Entry:
    def __init__(self, key: str, connect_kwargs: dict, on_event=None, on_subscriptions=None):
        self.key = key
        self.connect_kwargs = connect_kwargs
        self.on_event = on_event
        self.on_subscriptions = on_subscriptions
        self.queue: asyncio.Queue[_Request] = asyncio.Queue()
        self.ready = asyncio.get_running_loop().create_future()
        # Why the actor ended (exception), for supervisor logs.
        self.exit_reason: Optional[BaseException] = None
        self.task = asyncio.create_task(self._run(), name=f"persistent-mcp:{key}")

    async def _run(self):
        client = MCPClient()
        if self.on_event is not None:
            client.event_handler = partial(self.on_event, self.key)
        if self.on_subscriptions is not None:
            client.subscription_handler = partial(self.on_subscriptions, self.key)
        active: set[asyncio.Task] = set()

        async def execute(request):
            if request.future.cancelled():
                return
            task = asyncio.current_task()

            def caller_finished(future):
                if future.cancelled() and not task.done():
                    task.cancel()

            request.future.add_done_callback(caller_finished)
            try:
                result = await getattr(client, request.method)(
                    *request.args, **request.kwargs
                )
                if not request.future.done():
                    request.future.set_result(result)
            except BaseException as exc:
                if not request.future.done():
                    request.future.set_exception(exc)
            finally:
                request.future.remove_done_callback(caller_finished)

        try:
            await client.connect(**self.connect_kwargs)
            if not self.ready.done():
                self.ready.set_result(True)
            while True:
                if not _transport_alive(client):
                    raise TransportClosed(
                        f"Persistent MCP server {self.key} transport closed"
                    )
                try:
                    # Queue.get() is cancellation-safe: a timed-out get never
                    # consumes an item.
                    request = await asyncio.wait_for(
                        self.queue.get(), timeout=LIVENESS_POLL_SECONDS
                    )
                except TimeoutError:
                    continue
                if request.method == "__stop__":
                    if active:
                        await asyncio.gather(*active)
                    if not request.future.done():
                        request.future.set_result(True)
                    break
                operation = asyncio.create_task(execute(request))
                active.add(operation)
                operation.add_done_callback(active.discard)
        except BaseException as exc:
            self.exit_reason = exc
            if not self.ready.done():
                self.ready.set_exception(exc)
            while not self.queue.empty():
                request = self.queue.get_nowait()
                if not request.future.done():
                    request.future.set_exception(exc)
            if isinstance(exc, TransportClosed):
                log.warning(
                    "Persistent MCP server %s died (transport closed)", self.key
                )
            elif not isinstance(exc, asyncio.CancelledError):
                log.exception("Persistent MCP server %s stopped", self.key)
        finally:
            for operation in active:
                operation.cancel()
            if active:
                await asyncio.gather(*active, return_exceptions=True)
            await client.disconnect()

    def submit(self, method: str, *args, **kwargs) -> asyncio.Future:
        """Queue a request and make actor termination fail it, never strand it."""

        if self.task.done():
            # Retrieve the actor exception (if any) and present a useful error.
            exc = None if self.task.cancelled() else self.task.exception()
            raise RuntimeError(
                f"Persistent MCP server {self.key} is not running"
            ) from exc

        future = asyncio.get_running_loop().create_future()

        def actor_finished(task: asyncio.Task):
            if future.done():
                return
            exc = None if task.cancelled() else task.exception()
            error = RuntimeError(f"Persistent MCP server {self.key} is not running")
            if exc is not None:
                error.__cause__ = exc
            future.set_exception(error)

        def request_finished(_future: asyncio.Future):
            self.task.remove_done_callback(actor_finished)

        self.task.add_done_callback(actor_finished)
        future.add_done_callback(request_finished)
        self.queue.put_nowait(_Request(method, args, kwargs, future))
        return future

    async def stop(self):
        if self.task.done():
            try:
                await self.task
            except BaseException:
                pass
            return
        # Use the same termination callback as ordinary requests. If the owner
        # exits while disconnecting/draining, a stop waiter must not hang.
        future = self.submit("__stop__")
        await future
        await self.task


class PersistentMCPManager:
    def __init__(self, on_event=None, on_subscriptions=None):
        # async (process key, params): an openwebui/events push from a process.
        self._on_event = on_event
        # async (process key, items, call_meta): subscriptions a tool call changed.
        self._on_subscriptions = on_subscriptions
        self._entries: dict[str, _Entry] = {}
        self._connect_kwargs: dict[str, dict] = {}
        self._proxies: dict[str, PersistentMCPClient] = {}
        self._lifecycle_locks: dict[str, asyncio.Lock] = {}
        self._lock = asyncio.Lock()
        # Supervisor state: key -> [consecutive failures, current delay, next try (loop time)].
        self._retry: dict[str, list] = {}
        self._supervisor: Optional[asyncio.Task] = None
        self._closing = False

    async def _lifecycle_lock(self, key: str) -> asyncio.Lock:
        async with self._lock:
            lock = self._lifecycle_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._lifecycle_locks[key] = lock
            return lock

    def _proxy(self, key: str) -> PersistentMCPClient:
        # Caller holds ``self._lock``.
        proxy = self._proxies.get(key)
        if proxy is None:
            proxy = PersistentMCPClient(self, key)
            self._proxies[key] = proxy
        return proxy

    async def ensure(self, key: str, connect_kwargs: dict) -> PersistentMCPClient:
        lifecycle_lock = await self._lifecycle_lock(key)
        async with lifecycle_lock:
            async with self._lock:
                proxy = self._proxy(key)
                entry = self._entries.get(key)
                if entry is None or entry.task.done():
                    entry = _Entry(key, connect_kwargs, self._on_event, self._on_subscriptions)
                    self._entries[key] = entry
                    self._connect_kwargs[key] = connect_kwargs
                else:
                    # ``ensure`` deliberately reuses a healthy entry, so retain
                    # the configuration that actually created that entry too.
                    self._connect_kwargs.setdefault(key, entry.connect_kwargs)
            await entry.ready
            return proxy

    async def request(self, key: str, method: str, *args, **kwargs):
        """Submit an operation to the process currently serving ``key``.

        Submission happens while holding the manager lock.  A restart swaps the
        current entry under that same lock, so an operation is queued either on
        the old process before its stop marker or on the replacement after the
        swap; it can never land on a retired entry after the stop marker.
        """

        while True:
            async with self._lock:
                entry = self._entries.get(key)
                connect_kwargs = self._connect_kwargs.get(key)
                if entry is not None and not entry.task.done():
                    future = entry.submit(method, *args, **kwargs)
                    break

            # Unexpected actor/process failure: recover from the last known
            # configuration.  An explicit stop removes that configuration, so a
            # disabled or deleted connection is never resurrected by an old chat.
            if connect_kwargs is None:
                raise RuntimeError(f"Persistent MCP server {key} is not running")
            await self.ensure(key, connect_kwargs)

        return await future

    async def get(self, key: str) -> Optional[PersistentMCPClient]:
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry.task.done():
                return None
            proxy = self._proxy(key)
        await entry.ready
        return proxy

    async def stop(self, key: str):
        lifecycle_lock = await self._lifecycle_lock(key)
        async with lifecycle_lock:
            async with self._lock:
                entry = self._entries.pop(key, None)
                self._connect_kwargs.pop(key, None)
            if entry is not None:
                await entry.stop()

    async def restart(self, key: str, connect_kwargs: dict) -> PersistentMCPClient:
        """Replace a process without invalidating handles held by active turns.

        The replacement must initialize successfully before it becomes current.
        Once ready, the map swap is atomic with respect to ``request()``.  The
        retired actor receives its stop marker only after the swap, behind every
        operation that was already submitted to it.
        """

        lifecycle_lock = await self._lifecycle_lock(key)
        async with lifecycle_lock:
            replacement = _Entry(key, connect_kwargs, self._on_event, self._on_subscriptions)
            try:
                await replacement.ready
            except BaseException:
                await replacement.stop()
                raise

            async with self._lock:
                previous = self._entries.get(key)
                self._entries[key] = replacement
                self._connect_kwargs[key] = connect_kwargs
                proxy = self._proxy(key)

            if previous is not None and previous is not replacement:
                await previous.stop()
            return proxy

    async def ensure_many(
        self, items: Iterable[tuple[str, dict]]
    ) -> dict[str, Optional[BaseException]]:
        """``ensure`` every (key, connect_kwargs) concurrently; never raises.

        One hung initialize (MCP_INIT_TIMEOUT) no longer serializes boot.  A
        failed key stays in the desired map, so the supervisor retries it.
        Returns key -> exception (None on success).
        """

        items = list(items)
        results = await asyncio.gather(
            *(self.ensure(key, kwargs) for key, kwargs in items),
            return_exceptions=True,
        )
        outcome: dict[str, Optional[BaseException]] = {}
        for (key, _kwargs), result in zip(items, results):
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                log.error(
                    "Failed to start persistent MCP server %s (will retry): %r",
                    key,
                    result,
                )
                outcome[key] = result
            else:
                outcome[key] = None
        return outcome

    def start_supervisor(
        self, interval: float = SUPERVISE_INTERVAL_SECONDS
    ) -> asyncio.Task:
        """Start (idempotently) the background restart loop; returns its task."""

        if self._supervisor is None or self._supervisor.done():
            self._closing = False
            self._supervisor = asyncio.create_task(
                self.supervise(interval), name="persistent-mcp:supervisor"
            )
        return self._supervisor

    async def supervise(self, interval: float = SUPERVISE_INTERVAL_SECONDS):
        """Forever: every ``interval`` seconds, revive dead desired keys."""

        while not self._closing:
            try:
                await self.supervise_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Persistent MCP supervisor tick failed")
            await asyncio.sleep(interval)

    async def supervise_once(self):
        """One pass: restart every desired key whose actor is missing/dead.

        Desired = ``_connect_kwargs``.  ``stop()`` removes a key from it under
        the key's lifecycle lock, and ``_revive`` re-checks membership under
        that same lock, so a stopped/disabled/deleted key is never resurrected.
        """

        loop = asyncio.get_running_loop()
        now = loop.time()
        async with self._lock:
            if self._closing:
                return
            snapshot = [
                (key, self._entries.get(key)) for key in self._connect_kwargs
            ]
            # Forget backoff for keys no longer desired.
            for key in list(self._retry):
                if key not in self._connect_kwargs:
                    self._retry.pop(key, None)

        due = []
        for key, entry in snapshot:
            alive = entry is not None and not entry.task.done()
            state = self._retry.get(key)
            if alive:
                if (
                    state is not None
                    and entry.ready.done()
                    and not entry.ready.cancelled()
                    and entry.ready.exception() is None
                ):
                    # Recovered by someone else (lazy request, restart, ensure).
                    log.info("Persistent MCP server %s is running again", key)
                    self._retry.pop(key, None)
                continue
            if state is None:
                reason = entry.exit_reason if entry is not None else None
                log.warning(
                    "Persistent MCP server %s is not running (%s); restarting",
                    key,
                    repr(reason) if reason is not None else "no process",
                )
                state = [0, RESTART_BACKOFF_BASE_SECONDS, now]
                self._retry[key] = state
            if now >= state[2]:
                due.append(key)

        if due:
            await asyncio.gather(*(self._revive(key) for key in due))

    async def _revive(self, key: str):
        loop = asyncio.get_running_loop()
        lifecycle_lock = await self._lifecycle_lock(key)
        async with lifecycle_lock:
            async with self._lock:
                connect_kwargs = self._connect_kwargs.get(key)
                entry = self._entries.get(key)
                if self._closing or connect_kwargs is None:
                    self._retry.pop(key, None)
                    return
                if entry is None or entry.task.done():
                    entry = _Entry(
                        key, connect_kwargs, self._on_event, self._on_subscriptions
                    )
                    self._entries[key] = entry
            state = self._retry.setdefault(key, [0, RESTART_BACKOFF_BASE_SECONDS, 0.0])
            try:
                await entry.ready
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                state[0] += 1
                delay = state[1]
                state[1] = min(delay * 2, RESTART_BACKOFF_MAX_SECONDS)
                state[2] = loop.time() + delay
                log.warning(
                    "Persistent MCP server %s restart attempt %d failed: %r; "
                    "next attempt in %.0fs",
                    key,
                    state[0],
                    exc,
                    delay,
                )
                return
            attempts = state[0] + 1
            self._retry.pop(key, None)
            log.info(
                "Persistent MCP server %s recovered (attempt %d)", key, attempts
            )

    async def close(self):
        # Stop supervision first so it cannot respawn children mid-shutdown.
        self._closing = True
        supervisor, self._supervisor = self._supervisor, None
        if supervisor is not None and not supervisor.done():
            supervisor.cancel()
            try:
                await supervisor
            except BaseException:
                pass
        async with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
            self._connect_kwargs.clear()
            self._proxies.clear()
            self._lifecycle_locks.clear()
            self._retry.clear()
        for entry in reversed(entries):
            try:
                await entry.stop()
            except Exception:
                log.exception("Error stopping persistent MCP server %s", entry.key)


def personal_mcp_process_key(user_id: str, connection_id: str) -> str:
    return f"personal:{user_id}:{connection_id}"


def admin_mcp_process_key(server_id: str) -> str:
    return f"admin:{server_id}"
