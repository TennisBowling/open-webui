import asyncio
from types import SimpleNamespace

from open_webui.models.mcp import (
    MCPConnectionWithHeaders,
    MCPConnectionWithSecrets,
)
from open_webui.routers import mcp as mcp_router


def test_connection_list_returns_editable_fields_but_not_opaque_secrets(monkeypatch):
    connection = MCPConnectionWithSecrets(
        id="stdio-owner",
        user_id="owner",
        name="Local MCP",
        transport="stdio",
        auth_type="none",
        headers=[
            {"key": "X-Chat-Id", "value": "{{CHAT_ID}}"},
            {"key": "X-User-Id", "value": "{{USER_ID}}"},
        ],
        key="bearer-secret",
        env={"SECRET": "value"},
        oauth={"tokens": {"access_token": "oauth-secret"}},
        updated_at=1,
        created_at=1,
    )

    async def fake_get_connections(user_id, *, include_secrets=False, enabled_only=False):
        assert user_id == "owner"
        assert include_secrets is True
        return [connection]

    monkeypatch.setattr(
        mcp_router.MCPConnections,
        "get_connections_by_user_id",
        fake_get_connections,
    )

    result = asyncio.run(
        mcp_router.get_mcp_connections(user=SimpleNamespace(id="owner"))
    )

    assert result[0]["headers"] == connection.headers
    assert result[0]["env"] == connection.env
    assert "key" not in result[0]
    assert "oauth" not in result[0]
    # Match the route's response contract so a future response-model change
    # cannot silently strip the entries again.
    assert MCPConnectionWithHeaders.model_validate(result[0]).headers == connection.headers
