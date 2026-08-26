"""Long-lived stdio MCP processes owned by a single asyncio task.

AnyIO's stdio transport requires teardown in the task that opened it.  Each
entry below is therefore an actor: callers submit MCP operations through a
queue while the actor alone owns the MCPClient and its child process.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from open_webui.utils.mcp.client import MCPClient

log = logging.getLogger(__name__)


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
    def __init__(self, key: str, connect_kwargs: dict):
        self.key = key
        self.connect_kwargs = connect_kwargs
        self.queue: asyncio.Queue[_Request] = asyncio.Queue()
        self.ready = asyncio.get_running_loop().create_future()
        self.task = asyncio.create_task(self._run(), name=f"persistent-mcp:{key}")

    async def _run(self):
        client = MCPClient()
        try:
            await client.connect(**self.connect_kwargs)
            if not self.ready.done():
                self.ready.set_result(True)
            while True:
                request = await self.queue.get()
                if request.method == "__stop__":
                    if not request.future.done():
                        request.future.set_result(True)
                    break
                try:
                    result = await getattr(client, request.method)(
                        *request.args, **request.kwargs
                    )
                    if not request.future.done():
                        request.future.set_result(result)
                except BaseException as exc:
                    if not request.future.done():
                        request.future.set_exception(exc)
        except BaseException as exc:
            if not self.ready.done():
                self.ready.set_exception(exc)
            while not self.queue.empty():
                request = self.queue.get_nowait()
                if not request.future.done():
                    request.future.set_exception(exc)
            if not isinstance(exc, asyncio.CancelledError):
                log.exception("Persistent MCP server %s stopped", self.key)
        finally:
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
        future = asyncio.get_running_loop().create_future()
        await self.queue.put(_Request("__stop__", (), {}, future))
        await future
        await self.task


class PersistentMCPManager:
    def __init__(self):
        self._entries: dict[str, _Entry] = {}
        self._connect_kwargs: dict[str, dict] = {}
        self._proxies: dict[str, PersistentMCPClient] = {}
        self._lifecycle_locks: dict[str, asyncio.Lock] = {}
        self._lock = asyncio.Lock()

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
                    entry = _Entry(key, connect_kwargs)
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
            replacement = _Entry(key, connect_kwargs)
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

    async def close(self):
        async with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
            self._connect_kwargs.clear()
            self._proxies.clear()
            self._lifecycle_locks.clear()
        for entry in reversed(entries):
            await entry.stop()


def personal_mcp_process_key(user_id: str, connection_id: str) -> str:
    return f"personal:{user_id}:{connection_id}"


def admin_mcp_process_key(server_id: str) -> str:
    return f"admin:{server_id}"
