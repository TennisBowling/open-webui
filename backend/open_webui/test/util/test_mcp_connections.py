import asyncio
from types import SimpleNamespace

import open_webui.utils.mcp.connections as mcp_connections
from open_webui.utils.mcp.connections import (
    build_personal_mcp_connect_kwargs,
    parse_personal_mcp_tool_id,
    personal_mcp_tool_id,
    resolve_personal_mcp_call_meta,
    tool_allowed_by_policy,
    tool_filter_allows,
)


def test_tool_filter_absent_include_allows_everything():
    # No filter at all (fresh connection) exposes every tool, including future ones.
    assert tool_filter_allows({"name": "a"}, None)
    assert tool_filter_allows({"name": "a"}, {})
    # exclude-only (no allowlist) still allows non-excluded tools.
    assert tool_filter_allows({"name": "a"}, {"exclude": ["b"]})


def test_tool_filter_present_empty_include_blocks_all():
    # "Disable all" persists include: [] — an explicit empty allowlist = nothing
    # passes (it must NOT invert to "allow all").
    assert not tool_filter_allows({"name": "a"}, {"include": []})
    assert not tool_filter_allows({"name": "a"}, {"include": [], "exclude": []})


def test_tool_filter_include_is_allowlist():
    f = {"include": ["search", "fetch"]}
    assert tool_filter_allows({"name": "search"}, f)
    assert tool_filter_allows({"name": "fetch"}, f)
    # A tool not in the allowlist (e.g. one the server added later) is hidden.
    assert not tool_filter_allows({"name": "delete"}, f)


def test_tool_filter_exclude_is_denylist_and_wins():
    assert not tool_filter_allows({"name": "delete"}, {"exclude": ["delete"]})
    # exclude wins over include for the same name.
    assert not tool_filter_allows(
        {"name": "delete"}, {"include": ["delete"], "exclude": ["delete"]}
    )


def test_tool_filter_allows_drives_tool_allowed_by_policy():
    # The per-user policy check builds on the shared helper: an allowlist that
    # omits a read-only tool blocks it.
    conn = SimpleNamespace(tool_filters={"include": ["search"]}, policy={})
    assert tool_allowed_by_policy(
        {"name": "search", "annotations": {"readOnlyHint": True}}, conn
    )
    assert not tool_allowed_by_policy(
        {"name": "other", "annotations": {"readOnlyHint": True}}, conn
    )


def test_personal_mcp_tool_id_roundtrip():
    assert personal_mcp_tool_id("notion") == "user:mcp:notion"
    assert parse_personal_mcp_tool_id("user:mcp:notion") == "notion"
    assert parse_personal_mcp_tool_id("server:mcp:notion") is None


def test_personal_remote_mcp_resolves_live_chat_headers(monkeypatch):
    async def allow_test_url(*args, **kwargs):
        return None

    monkeypatch.setattr(mcp_connections, "validate_public_url", allow_test_url)
    connection = SimpleNamespace(
        id="canvas-personal",
        user_id="user-123",
        transport="remote_http",
        url="https://canvas.example.test/mcp",
        auth_type="none",
        key=None,
        headers=[
            {"key": "X-Chat-Id", "value": "{{CHAT_ID}}"},
            {"key": "X-Message-Id", "value": "{{MESSAGE_ID}}"},
            {"key": "X-User-Id", "value": "{{USER_ID}}"},
            {"key": "X-Static", "value": "canvas"},
            # Personal custom headers follow the admin-server safety rules.
            {"key": "Authorization", "value": "must-not-override-auth"},
        ],
        policy={},
        meta={},
        env={},
        args=[],
        command=None,
        cwd=None,
    )
    user = SimpleNamespace(id="user-123", name="Canvas User")

    kwargs = asyncio.run(
        build_personal_mcp_connect_kwargs(
            connection,
            user=user,
            metadata={"chat_id": "chat-456", "message_id": "message-789"},
        )
    )

    assert kwargs["headers"] == {
        "X-Chat-Id": "chat-456",
        "X-Message-Id": "message-789",
        "X-User-Id": "user-123",
        "X-Static": "canvas",
    }


def test_personal_stdio_mcp_resolves_dynamic_call_meta():
    connection = SimpleNamespace(
        id="ms365-personal",
        transport="stdio",
        headers=[
            {"key": "X-Chat-Id", "value": "{{CHAT_ID}}"},
            {"key": "X-Message-Id", "value": "{{MESSAGE_ID}}"},
            {"key": "X-User-Id", "value": "{{USER_ID}}"},
        ],
    )

    resolved = resolve_personal_mcp_call_meta(
        connection,
        user=SimpleNamespace(id="user-123", name="M365 User"),
        metadata={"chat_id": "chat-stdio", "message_id": "message-stdio"},
    )

    assert resolved == {
        "X-Chat-Id": "chat-stdio",
        "X-Message-Id": "message-stdio",
        "X-User-Id": "user-123",
    }


def test_tool_policy_allows_read_only_by_default():
    connection = SimpleNamespace(tool_filters={}, policy={})
    assert tool_allowed_by_policy(
        {"name": "search", "annotations": {"readOnlyHint": True}}, connection
    )


def test_tool_policy_blocks_unknown_write_by_default():
    connection = SimpleNamespace(tool_filters={}, policy={})
    assert not tool_allowed_by_policy({"name": "send-email"}, connection)


def test_tool_policy_allows_write_when_enabled():
    connection = SimpleNamespace(tool_filters={}, policy={"enable_write_tools": True})
    assert tool_allowed_by_policy(
        {"name": "send-email", "annotations": {"destructiveHint": True}}, connection
    )


def test_tool_policy_remote_command_cannot_bypass_write_gate():
    # E4: a REMOTE connection carrying a stray command must not get the stdio
    # auth/status name-whitelist (would let a malicious server slip a write tool
    # named "status" past the read-only gate).
    connection = SimpleNamespace(
        tool_filters={}, policy={}, transport="remote_http", command="x"
    )
    assert not tool_allowed_by_policy({"name": "status"}, connection)


def test_tool_policy_stdio_status_tool_allowed():
    connection = SimpleNamespace(
        tool_filters={}, policy={}, transport="stdio", command="x"
    )
    assert tool_allowed_by_policy({"name": "status"}, connection)
