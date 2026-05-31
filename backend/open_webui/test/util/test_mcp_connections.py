from types import SimpleNamespace

from open_webui.utils.mcp.connections import (
    parse_personal_mcp_tool_id,
    personal_mcp_tool_id,
    tool_allowed_by_policy,
)


def test_personal_mcp_tool_id_roundtrip():
    assert personal_mcp_tool_id("notion") == "user:mcp:notion"
    assert parse_personal_mcp_tool_id("user:mcp:notion") == "notion"
    assert parse_personal_mcp_tool_id("server:mcp:notion") is None


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
