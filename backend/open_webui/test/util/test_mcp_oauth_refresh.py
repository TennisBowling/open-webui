"""Live (Postgres-backed) regression tests for per-user MCP persistence + OAuth
refresh correctness (audit C1, A1-A5).

These exercise the real model/DB layer, so they need a reachable database
(``DATABASE_URL``). When none is configured they skip rather than fail, matching
the repo's Postgres-only runtime. Async work is driven via ``asyncio.run`` inside
sync test functions because no pytest-asyncio plugin is installed.
"""

import asyncio
import time
import uuid

import pytest

from open_webui.models.mcp import MCPConnections, MCPConnectionForm
from open_webui.utils.mcp import connections as C
from open_webui.utils.mcp.oauth import (
    MCPOAuthError,
    MCPOAuthHTTPError,
    MCPOAuthReauthRequired,
)


def _run(coro):
    async def _wrapped():
        # The global async engine pins its asyncpg pool to the first event loop
        # that uses it; each asyncio.run() here is a new loop, so drop stale
        # pooled connections first (close=False abandons them without touching
        # the old loop) to avoid "attached to a different loop" errors.
        from open_webui.internal.db import engine

        try:
            await engine.dispose(close=False)
        except Exception:
            pass
        return await coro

    return asyncio.run(_wrapped())


def _db_or_skip():
    try:
        _run(MCPConnections.get_connections_by_user_id("__probe__"))
    except Exception as exc:  # no DB reachable in this environment
        pytest.skip(f"database not available: {exc}")


@pytest.fixture()
def user_id():
    _db_or_skip()
    uid = f"mcp-test-{uuid.uuid4().hex[:8]}"
    yield uid
    for conn in _run(MCPConnections.get_connections_by_user_id(uid)):
        _run(MCPConnections.delete_connection_by_id_and_user_id(conn.id, uid))


def test_connection_persists_with_non_empty_json_columns(user_id):
    """C1: args/policy/tool_filters/meta must round-trip (jsonb columns)."""
    form = MCPConnectionForm(
        name="Roundtrip",
        transport="remote_http",
        url="https://example.com/mcp",
        auth_type="none",
        args=["--flag", "x"],
        policy={"enable_write_tools": True},
        tool_filters={"include": ["search", "fetch"]},
        meta={"template": "none", "k": "v"},
    )
    conn = _run(MCPConnections.insert_new_connection(user_id, form))
    assert conn is not None, "insert returned None — jsonb column write failed (C1)"
    got = _run(MCPConnections.get_connection_by_id_and_user_id(conn.id, user_id))
    assert got.args == ["--flag", "x"]
    assert got.policy == {"enable_write_tools": True}
    assert got.tool_filters == {"include": ["search", "fetch"]}
    assert got.meta.get("k") == "v"


def _make_oauth_connection(user_id, *, refresh="r1", access="old"):
    form = MCPConnectionForm(
        name="OAuth",
        transport="remote_http",
        url="https://mcp.example.com/mcp",
        auth_type="oauth_2.1",
    )
    conn = _run(MCPConnections.insert_new_connection(user_id, form))
    near = int(time.time()) + 10  # inside the 300s pre-expiry window
    _run(
        MCPConnections.update_oauth_by_id_and_user_id(
            conn.id,
            user_id,
            {
                "auth_metadata": {"token_endpoint": "https://mcp.example.com/token"},
                "client_info": {"client_id": "cid"},
                "resource_metadata": {"resource": "https://mcp.example.com/mcp"},
                "tokens": {
                    "access_token": access,
                    "refresh_token": refresh,
                    "expires_at": near,
                },
            },
        )
    )
    return _run(
        MCPConnections.get_connection_by_id_and_user_id(
            conn.id, user_id, include_secrets=True
        )
    )


def test_concurrent_refresh_is_serialized(user_id, monkeypatch):
    """A1: two concurrent resolves trigger exactly one refresh; rotation persists."""
    conn = _make_oauth_connection(user_id)
    calls = {"n": 0}

    async def fake_refresh(oauth, *, allow_localhost=False):
        calls["n"] += 1
        await asyncio.sleep(0.05)
        return {"access_token": f"new{calls['n']}", "refresh_token": "r2", "expires_in": 3600}

    monkeypatch.setattr(C, "refresh_token", fake_refresh)

    async def both():
        return await asyncio.gather(
            C.resolve_personal_bearer_token(conn),
            C.resolve_personal_bearer_token(conn),
        )

    results = _run(both())
    assert calls["n"] == 1, f"expected exactly one refresh, got {calls['n']}"
    assert results == ["new1", "new1"]
    after = _run(
        MCPConnections.get_connection_by_id_and_user_id(
            conn.id, user_id, include_secrets=True
        )
    )
    assert after.oauth["tokens"]["refresh_token"] == "r2"


def test_terminal_invalid_grant_clears_tokens(user_id, monkeypatch):
    """A3: invalid_grant clears stored tokens and raises a re-auth signal."""
    conn = _make_oauth_connection(user_id, refresh="rX")

    async def fake_refresh(oauth, *, allow_localhost=False):
        raise MCPOAuthHTTPError(400, '{"error":"invalid_grant"}', "https://mcp.example.com/token")

    monkeypatch.setattr(C, "refresh_token", fake_refresh)
    with pytest.raises(MCPOAuthReauthRequired):
        _run(C.resolve_personal_bearer_token(conn))
    after = _run(
        MCPConnections.get_connection_by_id_and_user_id(
            conn.id, user_id, include_secrets=True
        )
    )
    assert "tokens" not in (after.oauth or {})


def test_transient_refresh_preserves_tokens(user_id, monkeypatch):
    """A3: a transient (5xx) refresh error must not wipe credentials."""
    conn = _make_oauth_connection(user_id, refresh="rT")

    async def fake_refresh(oauth, *, allow_localhost=False):
        raise MCPOAuthHTTPError(503, "upstream down", "https://mcp.example.com/token")

    monkeypatch.setattr(C, "refresh_token", fake_refresh)
    with pytest.raises(MCPOAuthError):
        _run(C.resolve_personal_bearer_token(conn))
    after = _run(
        MCPConnections.get_connection_by_id_and_user_id(
            conn.id, user_id, include_secrets=True
        )
    )
    assert after.oauth.get("tokens", {}).get("refresh_token") == "rT"


def _set_clock_valid(conn, user_id, *, access, refresh):
    """Push the token's expiry far out so it is clock-valid (not near-expiry)."""
    _run(
        MCPConnections.merge_oauth_tokens_by_id_and_user_id(
            conn.id,
            user_id,
            {
                "access_token": access,
                "refresh_token": refresh,
                "expires_at": int(time.time()) + 3600,
            },
        )
    )
    return _run(
        MCPConnections.get_connection_by_id_and_user_id(
            conn.id, user_id, include_secrets=True
        )
    )


def test_midsession_401_refreshes_clock_valid_token(user_id, monkeypatch):
    """Round-2 bug 3: a server-side 401 on a still-clock-valid token must force a
    refresh (force_refresh), not hand back the just-rejected token."""
    conn = _make_oauth_connection(user_id, refresh="r401", access="rejected")
    conn = _set_clock_valid(conn, user_id, access="rejected", refresh="r401")
    calls = {"n": 0}

    async def fake_refresh(oauth, *, allow_localhost=False):
        calls["n"] += 1
        return {"access_token": "fresh", "refresh_token": "r402", "expires_in": 3600}

    monkeypatch.setattr(C, "refresh_token", fake_refresh)
    # _personal_oauth_refresh_cb is the mid-session-401 hook; it passes
    # force_refresh=True internally.
    got = _run(C._personal_oauth_refresh_cb(conn, "rejected"))
    assert calls["n"] == 1, "force_refresh must refresh even a clock-valid token"
    assert got == "fresh"


def test_resolve_path_keeps_expiry_shortcircuit(user_id, monkeypatch):
    """The expiry short-circuit (no force) still avoids a redundant refresh when a
    peer pushed the expiry out on the same access token — no regression."""
    conn = _make_oauth_connection(user_id, refresh="rP", access="A")
    conn = _set_clock_valid(conn, user_id, access="A", refresh="rP")
    calls = {"n": 0}

    async def fake_refresh(oauth, *, allow_localhost=False):
        calls["n"] += 1
        return {"access_token": "B", "expires_in": 3600}

    monkeypatch.setattr(C, "refresh_token", fake_refresh)
    # Direct call, force_refresh defaults False, current == stale "A", expiry
    # pushed out -> short-circuit without refreshing.
    got = _run(C._refresh_personal_token(conn, stale_token="A"))
    assert calls["n"] == 0, "resolve-path expiry short-circuit must not refresh"
    assert got == "A"


def test_authtype_change_clears_oauth_grant(user_id):
    """Round-8 bug A: changing auth_type (or url/transport) must clear the stored
    OAuth grant, so an owner can't launder a token to an attacker URL by flipping
    auth_type to 'none' and back."""
    import open_webui.routers.mcp as R
    from open_webui.routers.mcp import MCPConnectionUpdateForm
    from types import SimpleNamespace

    conn = _make_oauth_connection(user_id, refresh="rX", access="aX")
    before = _run(
        MCPConnections.get_connection_by_id_and_user_id(
            conn.id, user_id, include_secrets=True
        )
    )
    assert before.oauth.get("tokens", {}).get("access_token") == "aX"

    req = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(config=SimpleNamespace(USER_PERMISSIONS={}))
        )
    )
    owner = SimpleNamespace(id=user_id, role="admin")
    _run(
        R.update_mcp_connection(
            conn.id, req, MCPConnectionUpdateForm(auth_type="none"), owner
        )
    )
    after = _run(
        MCPConnections.get_connection_by_id_and_user_id(
            conn.id, user_id, include_secrets=True
        )
    )
    assert not (after.oauth or {}).get("tokens")


def test_persist_failure_clears_dead_token_and_forces_reauth(user_id, monkeypatch):
    """Round-6 bug B: if persisting a rotated token fails, the dead (rotated-away)
    token must be cleared + re-auth forced, not left in the DB to be replayed."""
    conn = _make_oauth_connection(user_id, refresh="rOld", access="aOld")

    async def fake_refresh(oauth, *, allow_localhost=False):
        return {"access_token": "aNew", "refresh_token": "rNew", "expires_in": 3600}

    async def fail_merge(_id, _uid, _tokens):
        return False

    monkeypatch.setattr(C, "refresh_token", fake_refresh)
    monkeypatch.setattr(
        MCPConnections, "merge_oauth_tokens_by_id_and_user_id", fail_merge
    )
    with pytest.raises(MCPOAuthReauthRequired):
        _run(C.resolve_personal_bearer_token(conn))
    after = _run(
        MCPConnections.get_connection_by_id_and_user_id(
            conn.id, user_id, include_secrets=True
        )
    )
    # The rotated-away token must be gone, not left to be replayed.
    assert "tokens" not in (after.oauth or {})


def test_malformed_2xx_refresh_clears_dead_token(user_id, monkeypatch):
    """Round-13: a 2xx refresh response with NO access_token may have consumed the
    (rotating) refresh token at the provider; clear it + force re-auth instead of
    leaving a possibly-dead token in the DB to be replayed."""
    conn = _make_oauth_connection(user_id, refresh="rM", access="aM")

    async def fake_refresh(oauth, *, allow_localhost=False):
        # 2xx but missing access_token (a non-compliant rotation response).
        return {"refresh_token": "rotated", "expires_in": 3600}

    monkeypatch.setattr(C, "refresh_token", fake_refresh)
    with pytest.raises(MCPOAuthReauthRequired):
        _run(C.resolve_personal_bearer_token(conn))
    after = _run(
        MCPConnections.get_connection_by_id_and_user_id(
            conn.id, user_id, include_secrets=True
        )
    )
    assert "tokens" not in (after.oauth or {})


def test_user_patch_cannot_reenable_admin_disabled(user_id):
    """Round-6 bug A: the admin kill switch is authoritative — a user PATCH cannot
    flip `enabled` back on (only the admin enable endpoint can)."""
    import open_webui.routers.mcp as R
    from open_webui.routers.mcp import MCPConnectionUpdateForm
    from types import SimpleNamespace

    form = MCPConnectionForm(
        name="K",
        transport="remote_http",
        url="https://mcp.example.com/mcp",
        auth_type="none",
    )
    conn = _run(MCPConnections.insert_new_connection(user_id, form))
    _run(MCPConnections.set_enabled_by_id(conn.id, False))  # admin kill switch
    req = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(config=SimpleNamespace(USER_PERMISSIONS={}))
        )
    )
    # admin role only so _validate_form passes; the fix drops `enabled` regardless.
    owner = SimpleNamespace(id=user_id, role="admin")
    _run(
        R.update_mcp_connection(conn.id, req, MCPConnectionUpdateForm(enabled=True), owner)
    )
    after = _run(MCPConnections.get_connection_by_id_and_user_id(conn.id, user_id))
    assert after.enabled is False


def test_merge_oauth_fields_preserves_tokens(user_id):
    """Round-3 bug 2: the OAuth /start field-merge must NOT clobber a concurrently
    -rotated token blob (it goes through the SELECT..FOR UPDATE re-read and only
    touches the auth-flow keys)."""
    conn = _make_oauth_connection(user_id, refresh="r-keep", access="tok-keep")
    ok = _run(
        MCPConnections.merge_oauth_fields_by_id_and_user_id(
            conn.id,
            user_id,
            {"state": "S", "code_verifier": "V", "client_info": {"client_id": "cid2"}},
            ["return_to"],
        )
    )
    assert ok
    after = _run(
        MCPConnections.get_connection_by_id_and_user_id(
            conn.id, user_id, include_secrets=True
        )
    )
    # Tokens preserved untouched; new auth-flow fields applied.
    assert after.oauth["tokens"]["refresh_token"] == "r-keep"
    assert after.oauth["tokens"]["access_token"] == "tok-keep"
    assert after.oauth["state"] == "S"
    assert after.oauth["code_verifier"] == "V"
    assert after.oauth["client_info"] == {"client_id": "cid2"}


def test_disabled_connection_blocks_user_endpoints(user_id):
    """Round-2 bug 4: an admin-disabled connection is not usable via the owner's
    verify / tools / oauth-start endpoints (the kill switch is authoritative)."""
    import open_webui.routers.mcp as R
    from fastapi import HTTPException
    from types import SimpleNamespace

    form = MCPConnectionForm(
        name="Disabled",
        transport="remote_http",
        url="https://mcp.example.com/mcp",
        auth_type="none",
    )
    conn = _run(MCPConnections.insert_new_connection(user_id, form))
    _run(MCPConnections.set_enabled_by_id(conn.id, False))
    fake_user = SimpleNamespace(id=user_id, role="user")
    fake_req = SimpleNamespace()
    for call in (
        lambda: R.verify_mcp_connection(conn.id, fake_req, fake_user),
        lambda: R.get_mcp_connection_tools(conn.id, fake_req, user=fake_user),
        lambda: R.start_mcp_oauth(conn.id, fake_req, None, fake_user),
    ):
        with pytest.raises(HTTPException) as ei:
            _run(call())
        assert ei.value.status_code == 403
