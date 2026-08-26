import asyncio
import logging
import os
from typing import Optional
from contextlib import AsyncExitStack

import anyio
import httpx

from mcp import ClientSession, StdioServerParameters
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamablehttp_client
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from open_webui.utils.tool_calling import mcp_tool_alias
from open_webui.utils.mcp.oauth import guarded_httpx_client_factory
from open_webui.env import SRC_LOG_LEVELS

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])


# Cold remote initialize (DNS + TLS + server cold-start) can exceed a tight
# bound, so default to 30s (the SDK's own httpx default) and allow override.
MCP_INIT_TIMEOUT = int(os.environ.get("MCP_INIT_TIMEOUT", "30"))
# Default tool-call bound: 0 = UNLIMITED. Long-running tools (deep-analysis
# containers, bash, browser sessions, web research) must run until they finish;
# a hidden client-side cap surfaced as "random" subagent tool crashes. The
# model's own `timeout` tool argument (when it passes one) still produces a
# backstop bound — see _model_requested_timeout_seconds — and a dead peer is
# surfaced by TCP keepalive on the transport, so unlimited ≠ hangs forever.
MCP_CALL_TIMEOUT = int(os.environ.get("MCP_CALL_TIMEOUT", "0"))
# Tool DISCOVERY must not share the long tool-execution budget — a wedged server
# would otherwise block chat setup for the full call timeout.
MCP_TOOL_LIST_TIMEOUT = int(os.environ.get("MCP_TOOL_LIST_TIMEOUT", "45"))
MCP_CALL_TIMEOUT_EXEMPT_TOOLS = {
    name.strip()
    for name in os.environ.get("MCP_CALL_TIMEOUT_EXEMPT_TOOLS", "bash,web_search,web_fetch").split(",")
    if name.strip()
}
# NOTE: session teardown (the streamable-http DELETE in disconnect()) is NOT
# wrapped in an anyio/asyncio timeout here — interrupting stack.aclose() mid-flight
# breaks the transport's cancel-scope LIFO nesting (see the long comment in
# disconnect()). The teardown DELETE is instead bounded per-request inside
# _SSRFGuardedAsyncTransport (MCP_TEARDOWN_TIMEOUT in utils/mcp/oauth.py) — the
# client-level read timeout is unbounded on purpose for long tool calls, so it
# cannot serve as the teardown bound.


def _model_requested_timeout_seconds(function_args: Optional[dict]) -> Optional[float]:
    """The timeout the MODEL asked for inside the tool arguments, if any.

    The tool server is the authority for enforcing its own timeout argument
    (e.g. the container bash tool stops the command and returns an in-band
    timeout result). The client only uses this value to size a BACKSTOP bound
    a comfortable margin above the model's budget: when the server enforces
    its argument, the server's result always wins the race; when it doesn't,
    the call can't hang forever past what the model intended.

    Units: ``*_ms`` keys are milliseconds; bare ``timeout``/``timeout_seconds``
    are read as seconds. Misreading a milliseconds value as seconds only
    LOOSENS the backstop — it can never truncate a call early, which is the
    safe direction.
    """
    if not function_args:
        return None

    def _positive_number(value) -> Optional[float]:
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    for key in ("timeout", "timeout_seconds", "timeoutSeconds", "timeout_sec"):
        seconds = _positive_number(function_args.get(key))
        if seconds is not None:
            return seconds
    for key in ("timeout_ms", "timeoutMs"):
        ms = _positive_number(function_args.get(key))
        if ms is not None:
            return ms / 1000.0
    return None


class BearerRefreshAuth(httpx.Auth):
    """Inject a bearer token and refresh-on-401 for streamable-HTTP MCP.

    The static-header approach freezes the access token at connect time, so a
    long agentic turn that outlives the token lifetime starts 401-ing on every
    tool call (audit A6). This auth flow asks ``refresh_cb`` (which performs a
    serialized, persisted refresh) for a new token on a 401 and retries once.
    """

    def __init__(self, token: Optional[str], refresh_cb):
        self._token = token
        self._refresh_cb = refresh_cb  # async (stale_token) -> Optional[str]

    async def async_auth_flow(self, request):
        if self._token:
            request.headers["Authorization"] = f"Bearer {self._token}"
        response = yield request
        if response.status_code == 401 and self._refresh_cb is not None:
            stale = self._token
            try:
                new_token = await self._refresh_cb(stale)
            except Exception:
                new_token = None
            if new_token and new_token != stale:
                self._token = new_token
                request.headers["Authorization"] = f"Bearer {new_token}"
                yield request

def build_mcp_connect_kwargs(
    connection: dict,
    *,
    bearer_token: Optional[str],
    user=None,
    metadata: Optional[dict] = None,
) -> dict:
    """Single source of truth for ``MCPClient.connect(**kwargs)`` arguments.

    The verify endpoint, the runtime middleware, and the subagent runner all
    used to assemble the same shape of headers, stdio detection, and
    custom-header merge inline. They drifted (verify never tested stdio;
    middleware had a slightly different rule for "send headers only when no
    command"), which meant verify could pass while runtime broke and vice
    versa. Centralizing it kills the drift.

    The caller resolves the bearer token because the *source* differs per
    site (verify reads cookies / oauth_manager; runtime has a pre-fetched
    ``__oauth_token__``); everything else is the same.

    Reserved headers (``Authorization``, ``Content-Type``, ...) are dropped
    from custom headers by ``resolve_tool_server_headers``, so the
    ``Authorization`` set here can't be clobbered by misconfiguration.
    """
    # Late import: this module is imported at server startup by middleware,
    # and utils.tools pulls in heavy deps. Keeping it lazy avoids the cycle
    # and the cost.
    from open_webui.utils.tools import resolve_tool_server_headers

    mcp_config = connection.get("config") or {}
    command = mcp_config.get("command")
    args = mcp_config.get("args")
    env = mcp_config.get("env")
    cwd = mcp_config.get("cwd")

    headers: dict = {}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"

    try:
        custom_headers = resolve_tool_server_headers(
            connection, user=user, metadata=metadata
        )
    except Exception:
        custom_headers = None
    if custom_headers:
        headers = {**headers, **custom_headers}

    if command:
        # stdio: URL and HTTP headers are meaningless.
        return {
            "command": command,
            "args": args,
            "env": env,
            "cwd": cwd,
            "transport": "stdio",
        }

    return {
        "url": connection.get("url", "") or None,
        "headers": headers or None,
        "transport": connection.get("transport") or "remote_http",
        # The MCP SDK default HTTP client has a 300s read timeout. Use our
        # factory so long-running exempt tools (bash/web_search/web_fetch) are not
        # cancelled below the Open WebUI tool-timeout layer. Admin-configured MCP
        # timeouts still apply in MCPClient.call_tool for non-exempt tools.
        "httpx_client_factory": guarded_httpx_client_factory(allow_localhost=True),
    }


class MCPClient:
    def __init__(self):
        self.session: Optional[ClientSession] = None
        self.exit_stack: Optional[AsyncExitStack] = None

    async def connect(
        self,
        url: Optional[str] = None,
        headers: Optional[dict] = None,
        command: Optional[str] = None,
        args: Optional[list[str]] = None,
        env: Optional[dict[str, str]] = None,
        cwd: Optional[str] = None,
        transport: Optional[str] = None,
        auth: Optional[httpx.Auth] = None,
        httpx_client_factory=None,
    ):
        transport_type = transport or "remote_http"
        # Take ownership of the exit stack up front so the cancel-scope-protected
        # disconnect() is the cleanup path even when connect() fails BEFORE init
        # completes (e.g. an init timeout or a CancelledError mid-handshake).
        # Reuse a stack already entered by __aenter__ (context-manager form) so it
        # isn't abandoned/leaked.
        exit_stack = self.exit_stack
        if exit_stack is None:
            exit_stack = AsyncExitStack()
            await exit_stack.__aenter__()
            self.exit_stack = exit_stack
        try:
            if command:
                server_params = StdioServerParameters(
                    command=command,
                    args=args or [],
                    env=env,
                    cwd=cwd,
                )
                streams_context = stdio_client(server_params)
            elif url:
                if transport_type == "remote_sse":
                    streams_context = sse_client(
                        url,
                        headers=headers,
                        auth=auth,
                        **(
                            {"httpx_client_factory": httpx_client_factory}
                            if httpx_client_factory is not None
                            else {}
                        ),
                    )
                else:
                    streams_context = streamablehttp_client(
                        url,
                        headers=headers,
                        auth=auth,
                        **(
                            {"httpx_client_factory": httpx_client_factory}
                            if httpx_client_factory is not None
                            else {}
                        ),
                    )
            else:
                raise ValueError("Either url or command must be provided")

            transport_result = await exit_stack.enter_async_context(streams_context)

            if command or transport_type == "remote_sse":
                read_stream, write_stream = transport_result
            else:
                # `streamablehttp_client()` return signature has changed across MCP
                # releases (either 2-tuple or 3-tuple). Handle both.
                try:
                    read_stream, write_stream = transport_result
                except ValueError:
                    read_stream, write_stream, _ = transport_result

            session = await exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            with anyio.fail_after(MCP_INIT_TIMEOUT):
                await session.initialize()

            self.session = session
        except BaseException:
            # Includes CancelledError. disconnect() owns the cancel-scope-safe
            # teardown (the stack was transferred to self above).
            await self.disconnect()
            raise

    async def list_tool_specs(self) -> Optional[list[dict]]:
        if not self.session:
            raise RuntimeError("MCP client is not connected.")

        tool_specs: list[dict] = []
        cursor: Optional[str] = None
        # Servers can paginate large tool catalogs since MCP spec 2025-03-26
        # (SDK 1.15+ ships paginated decorators for the server side). Loop
        # until nextCursor stops moving so we surface the entire catalog,
        # not just the first page.
        with anyio.fail_after(MCP_TOOL_LIST_TIMEOUT):
            while True:
                result = await self.session.list_tools(cursor=cursor)
                tools = result.tools or []

                for tool in tools:
                    name = tool.name
                    description = tool.description

                    inputSchema = tool.inputSchema

                    outputSchema = getattr(tool, "outputSchema", None)
                    annotations = getattr(tool, "annotations", None)
                    title = getattr(tool, "title", None)

                    spec = {"name": name, "description": description, "parameters": inputSchema}
                    if outputSchema:
                        spec["outputSchema"] = outputSchema
                    if annotations:
                        spec["annotations"] = annotations.model_dump(mode="json") if hasattr(annotations, "model_dump") else annotations
                    if title:
                        spec["title"] = title
                    tool_specs.append(spec)

                next_cursor = getattr(result, "nextCursor", None)
                if not next_cursor or next_cursor == cursor:
                    break
                cursor = next_cursor

        return tool_specs

    async def call_tool(
        self,
        function_name: str,
        function_args: dict,
        timeout_seconds: Optional[int] = None,
        timeout_exempt_tools: Optional[set[str]] = None,
        meta: Optional[dict] = None,
    ) -> Optional[dict]:
        if not self.session:
            raise RuntimeError("MCP client is not connected.")

        exempt_tools = timeout_exempt_tools or MCP_CALL_TIMEOUT_EXEMPT_TOOLS
        # Bound precedence: the model's own timeout argument (plus a backstop
        # margin) > exempt tools run unbounded > admin-configured cap > default
        # (unlimited). A model-declared budget deliberately outranks the admin
        # cap — when the model says a command may take 2 hours, cutting it off
        # at an unrelated cap crashes the run for no benefit.
        model_timeout = _model_requested_timeout_seconds(function_args)
        if model_timeout is not None:
            timeout = model_timeout + max(60.0, model_timeout * 0.25)
        elif function_name in exempt_tools:
            timeout = 0
        else:
            timeout = MCP_CALL_TIMEOUT if timeout_seconds is None else int(timeout_seconds)

        call_kwargs = {"meta": meta} if meta else {}
        if timeout <= 0:
            result = await self.session.call_tool(
                function_name, function_args, **call_kwargs
            )
        else:
            with anyio.fail_after(timeout):
                result = await self.session.call_tool(
                    function_name, function_args, **call_kwargs
                )
        if not result:
            raise Exception("No result returned from MCP tool call.")

        result_dict = result.model_dump(mode="json")
        result_content = result_dict.get("content", {})
        if result.isError:
            return {
                "isError": True,
                "content": result_content,
            }
        return result_content

    async def list_resources(self, cursor: Optional[str] = None) -> Optional[dict]:
        if not self.session:
            raise RuntimeError("MCP client is not connected.")

        result = await self.session.list_resources(cursor=cursor)
        if not result:
            raise Exception("No result returned from MCP list_resources call.")

        result_dict = result.model_dump()
        resources = result_dict.get("resources", [])

        return resources

    async def read_resource(self, uri: str) -> Optional[dict]:
        if not self.session:
            raise RuntimeError("MCP client is not connected.")

        result = await self.session.read_resource(uri)
        if not result:
            raise Exception("No result returned from MCP read_resource call.")
        result_dict = result.model_dump()

        return result_dict

    async def disconnect(self):
        # Clean up and close the session. Safe to call before/without a
        # successful connect() — exit_stack is None until ownership is
        # transferred from connect()'s local stack.
        if self.exit_stack is None:
            return
        stack = self.exit_stack
        self.exit_stack = None
        self.session = None

        # MCP streamable-http transports use AnyIO cancel scopes inside async
        # generators. Those scopes must be exited by the SAME asyncio task that
        # entered them. During Ctrl+C/server shutdown this task may already have
        # pending cancellation, and letting that cancellation interrupt aclose()
        # leaves the async generator to be finalized later by loop.shutdown_asyncgens
        # in a different task, which raises:
        # "Attempted to exit cancel scope in a different task than it was entered in".
        # Temporarily clear this task's pending cancellation so the transport can
        # close in-place, then restore the cancellation count for the caller.
        task = asyncio.current_task()
        uncancelled = 0
        if task is not None and hasattr(task, "uncancel"):
            while task.cancelling():
                task.uncancel()
                uncancelled += 1

        try:
            # NOTE: do NOT wrap this in anyio.move_on_after / fail_after — the
            # transport's own anyio cancel scopes were entered in connect(), and
            # introducing another cancel scope here violates LIFO nesting and
            # raises "Attempted to exit cancel scope ..." on every disconnect.
            await stack.aclose()
        finally:
            if task is not None:
                for _ in range(uncancelled):
                    task.cancel()

    async def __aenter__(self):
        # Lazily allocated so __aexit__'s disconnect() finds a stack to close.
        if self.exit_stack is None:
            self.exit_stack = AsyncExitStack()
            await self.exit_stack.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.disconnect()


async def disconnect_mcp_clients(mcp_clients, *, context: str = "cleanup") -> None:
    """Tear down a turn's MCP clients in REVERSE connection order.

    This ordering is not a preference, it is the transports' requirement. Clients
    are connected serially in one task (``setup_mcp_tools``), so each transport's
    AnyIO cancel scopes nest *inside* the previous client's. AnyIO enforces LIFO
    unwinding: exiting an outer scope while an inner one is still open raises

        RuntimeError: Attempted to exit a cancel scope that isn't the current
        task's current cancel scope

    ...and leaves the stack half-unwound — the stdio subprocess and the transport
    task group are abandoned, still running.

    Both call sites used to iterate ``mcp_clients.items()`` directly, i.e. in
    INSERTION order, which is the exact opposite. It only bites with two or more
    servers attached to one turn, which is what made it look intermittent.

    Every client is attempted even if one fails, so a single bad transport cannot
    strand the rest.
    """
    if not mcp_clients:
        return
    for server_id, client in reversed(list(mcp_clients.items())):
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001 — teardown must never raise onward
            log.exception(
                "Error disconnecting MCP client %r during %s", server_id, context
            )
