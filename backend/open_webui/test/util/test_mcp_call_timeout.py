from contextlib import contextmanager

from open_webui.utils.mcp import client as mcp_client


class _Result:
    isError = False

    def model_dump(self, mode="json"):
        return {"content": [{"type": "text", "text": "ok"}]}


class _Session:
    def __init__(self):
        self.calls = []

    async def call_tool(self, function_name, function_args, *, meta=None):
        self.calls.append((function_name, function_args, meta))
        return _Result()


async def _call(name: str):
    c = mcp_client.MCPClient()
    c.session = _Session()
    return await c.call_tool(name, {"x": 1})


async def _call_with_timeout(name: str, timeout_seconds: int):
    c = mcp_client.MCPClient()
    c.session = _Session()
    return await c.call_tool(name, {"x": 1}, timeout_seconds=timeout_seconds)


def test_mcp_call_timeout_exempt_tools_do_not_enter_fail_after(monkeypatch):
    entered = []

    @contextmanager
    def fake_fail_after(timeout):
        entered.append(timeout)
        yield

    monkeypatch.setattr(mcp_client.anyio, "fail_after", fake_fail_after)

    import asyncio

    assert asyncio.run(_call("bash")) == [{"type": "text", "text": "ok"}]
    assert asyncio.run(_call("web_search")) == [{"type": "text", "text": "ok"}]
    assert asyncio.run(_call("web_fetch")) == [{"type": "text", "text": "ok"}]
    assert entered == []


def test_mcp_call_timeout_still_wraps_other_tools(monkeypatch):
    entered = []

    @contextmanager
    def fake_fail_after(timeout):
        entered.append(timeout)
        yield

    monkeypatch.setattr(mcp_client.anyio, "fail_after", fake_fail_after)
    monkeypatch.setattr(mcp_client, "MCP_CALL_TIMEOUT", 123)

    import asyncio

    assert asyncio.run(_call("other_tool")) == [{"type": "text", "text": "ok"}]
    assert entered == [123]


def test_mcp_call_timeout_uses_admin_configured_value(monkeypatch):
    entered = []

    @contextmanager
    def fake_fail_after(timeout):
        entered.append(timeout)
        yield

    monkeypatch.setattr(mcp_client.anyio, "fail_after", fake_fail_after)

    import asyncio

    assert asyncio.run(_call_with_timeout("other_tool", 42)) == [
        {"type": "text", "text": "ok"}
    ]
    assert entered == [42]


def test_mcp_call_timeout_zero_disables_for_non_exempt_tools(monkeypatch):
    entered = []

    @contextmanager
    def fake_fail_after(timeout):
        entered.append(timeout)
        yield

    monkeypatch.setattr(mcp_client.anyio, "fail_after", fake_fail_after)

    import asyncio

    assert asyncio.run(_call_with_timeout("other_tool", 0)) == [
        {"type": "text", "text": "ok"}
    ]
    assert entered == []


def test_mcp_call_timeout_default_is_unlimited(monkeypatch):
    # No admin value, no model arg, non-exempt tool: must NOT be bounded.
    entered = []

    @contextmanager
    def fake_fail_after(timeout):
        entered.append(timeout)
        yield

    monkeypatch.setattr(mcp_client.anyio, "fail_after", fake_fail_after)

    import asyncio

    async def _run():
        c = mcp_client.MCPClient()
        c.session = _Session()
        return await c.call_tool("deep_analysis", {"query": "x"})

    assert asyncio.run(_run()) == [{"type": "text", "text": "ok"}]
    assert entered == []


def test_mcp_call_forwards_request_meta():
    import asyncio

    async def _run():
        c = mcp_client.MCPClient()
        session = _Session()
        c.session = session
        result = await c.call_tool(
            "mail_search",
            {"query": "hello"},
            meta={"X-Chat-Id": "chat-123", "X-User-Id": "user-456"},
        )
        return result, session.calls

    result, calls = asyncio.run(_run())
    assert result == [{"type": "text", "text": "ok"}]
    assert calls == [
        (
            "mail_search",
            {"query": "hello"},
            {"X-Chat-Id": "chat-123", "X-User-Id": "user-456"},
        )
    ]


def test_mcp_call_model_timeout_arg_outranks_admin_cap(monkeypatch):
    # The model asked for 2 hours; an admin cap of 42s must not truncate it.
    # The client backstop is the model's budget plus margin (>= its budget).
    entered = []

    @contextmanager
    def fake_fail_after(timeout):
        entered.append(timeout)
        yield

    monkeypatch.setattr(mcp_client.anyio, "fail_after", fake_fail_after)

    import asyncio

    async def _run():
        c = mcp_client.MCPClient()
        c.session = _Session()
        return await c.call_tool(
            "deep_analysis", {"query": "x", "timeout": 7200}, timeout_seconds=42
        )

    assert asyncio.run(_run()) == [{"type": "text", "text": "ok"}]
    assert entered == [7200 + 7200 * 0.25]


def test_mcp_call_model_timeout_arg_bounds_exempt_tools_too(monkeypatch):
    # bash with an explicit model timeout gets the backstop instead of running
    # unbounded — "whatever timeout the model set in that tool call".
    entered = []

    @contextmanager
    def fake_fail_after(timeout):
        entered.append(timeout)
        yield

    monkeypatch.setattr(mcp_client.anyio, "fail_after", fake_fail_after)

    import asyncio

    async def _run():
        c = mcp_client.MCPClient()
        c.session = _Session()
        return await c.call_tool("bash", {"command": "sleep 5", "timeout": 30})

    assert asyncio.run(_run()) == [{"type": "text", "text": "ok"}]
    # small budgets get the 60s floor margin
    assert entered == [30 + 60.0]


def test_model_requested_timeout_parsing():
    parse = mcp_client._model_requested_timeout_seconds
    assert parse(None) is None
    assert parse({}) is None
    assert parse({"timeout": 120}) == 120
    assert parse({"timeout_seconds": "90"}) == 90
    assert parse({"timeout_ms": 5000}) == 5.0
    # bools, zero, negatives and garbage are not timeouts
    assert parse({"timeout": True}) is None
    assert parse({"timeout": 0}) is None
    assert parse({"timeout": -5}) is None
    assert parse({"timeout": "soon"}) is None


def test_admin_mcp_connect_kwargs_use_unbounded_read_timeout_factory():
    kwargs = mcp_client.build_mcp_connect_kwargs(
        {"url": "http://127.0.0.1:8080/mcp", "transport": "remote_http"},
        bearer_token=None,
    )

    factory = kwargs["httpx_client_factory"]

    async def _check():
        client = factory(headers=None, timeout=None, auth=None)
        try:
            assert client.timeout.read is None
        finally:
            await client.aclose()

    import asyncio

    asyncio.run(_check())


def test_admin_mcp_connect_factory_overrides_sdk_default_read_timeout():
    import httpx

    kwargs = mcp_client.build_mcp_connect_kwargs(
        {"url": "http://127.0.0.1:8080/mcp", "transport": "remote_http"},
        bearer_token=None,
    )

    factory = kwargs["httpx_client_factory"]

    async def _check():
        client = factory(
            headers=None,
            timeout=httpx.Timeout(30.0, read=300.0),
            auth=None,
        )
        try:
            assert client.timeout.connect == 30.0
            assert client.timeout.write == 30.0
            assert client.timeout.pool == 30.0
            assert client.timeout.read is None
        finally:
            await client.aclose()

    import asyncio

    asyncio.run(_check())
