import asyncio
import os
import stat
import time
from pathlib import Path
from typing import Optional

from open_webui.env import DATA_DIR
from open_webui.models.mcp import MCPConnectionWithSecrets, MCPConnections
from open_webui.utils.mcp.oauth import (
    MCPOAuthError,
    MCPOAuthReauthRequired,
    guarded_httpx_client_factory,
    is_terminal_token_error,
    refresh_token,
    token_expires_at,
    validate_public_url,
)
from open_webui.utils.tools import resolve_tool_server_headers


PERSONAL_MCP_PREFIX = "user:mcp:"


# Per-connection refresh serialization. Notion (and OAuth 2.1 generally) rotates
# the refresh token on every use and revokes the whole grant if a rotated-away
# token is replayed. Concurrent chats / parallel subagents / the tools+verify
# endpoints all resolve the same connection's token independently, so without
# serialization two of them refresh with the same refresh token and kill the
# grant. We serialize the refresh (network call + persist) per connection id
# within the process; the persist itself is additionally row-locked in the model
# layer. (Cross-worker serialization would need a distributed lock — deferred
# with the multi-worker rollout; the runtime is single-worker today.)
_refresh_locks: dict[str, asyncio.Lock] = {}
_refresh_locks_guard = asyncio.Lock()


async def _get_refresh_lock(connection_id: str) -> asyncio.Lock:
    async with _refresh_locks_guard:
        # Opportunistically evict idle locks so the registry can't grow without
        # bound (e.g. after many connections are created/deleted).
        if len(_refresh_locks) > 1024:
            for key in [k for k, v in _refresh_locks.items() if not v.locked()]:
                _refresh_locks.pop(key, None)
        lock = _refresh_locks.get(connection_id)
        if lock is None:
            lock = asyncio.Lock()
            _refresh_locks[connection_id] = lock
        return lock


def discard_refresh_lock(connection_id: str) -> None:
    """Drop a connection's refresh lock (called when the connection is deleted)."""
    lock = _refresh_locks.get(connection_id)
    if lock is not None and not lock.locked():
        _refresh_locks.pop(connection_id, None)


STDIO_TEMPLATES: dict[str, dict] = {
    "outlook-assistant": {
        "name": "Outlook Assistant",
        "command": "npx",
        "args": ["-y", "@littlebearapps/outlook-assistant"],
        "env": {
            "OUTLOOK_AUTH_METHOD": "device-code",
            "OUTLOOK_MAX_EMAILS_PER_SESSION": "10",
        },
        "node_stdio_sanitize": True,
    },
    "slack": {
        "name": "Slack",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-slack"],
        "env": {},
    },
    "gdrive": {
        "name": "Google Drive",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-gdrive"],
        "env": {"GDRIVE_CREDENTIALS_PATH": "{{MCP_HOME}}/gdrive-credentials.json"},
    },
    "filesystem": {
        "name": "Filesystem",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "{{MCP_HOME}}/workspace"],
        "env": {},
    },
    "postgres": {
        "name": "Postgres",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-postgres"],
        "env": {},
    },
}


def personal_mcp_tool_id(connection_id: str) -> str:
    return f"{PERSONAL_MCP_PREFIX}{connection_id}"


def parse_personal_mcp_tool_id(tool_id: str) -> Optional[str]:
    if tool_id.startswith(PERSONAL_MCP_PREFIX):
        return tool_id[len(PERSONAL_MCP_PREFIX) :]
    return None


def mcp_home(user_id: str, connection_id: str) -> Path:
    base = DATA_DIR / "mcp" / "users" / user_id / connection_id / "home"
    base.mkdir(parents=True, exist_ok=True)
    try:
        base.chmod(0o700)
    except Exception:
        pass
    return base


def _substitute(value, home: Path):
    if isinstance(value, str):
        return value.replace("{{MCP_HOME}}", str(home))
    if isinstance(value, list):
        return [_substitute(v, home) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, home) for k, v in value.items()}
    return value


def _write_node_stdio_preload(home: Path) -> str:
    path = home / ".open-webui-node-stdio-sanitize.cjs"
    if not path.exists():
        path.write_text(
            "const toErr = (method) => (...args) => console.error(...args);\n"
            "console.log = toErr('log');\n"
            "console.warn = toErr('warn');\n",
            encoding="utf-8",
        )
        try:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except Exception:
            pass
    return str(path)


async def resolve_personal_bearer_token(
    connection: MCPConnectionWithSecrets,
) -> Optional[str]:
    if connection.auth_type == "bearer":
        return connection.key or None
    if connection.auth_type != "oauth_2.1":
        return None

    oauth = connection.oauth or {}
    tokens = oauth.get("tokens") or {}
    access_token = tokens.get("access_token")
    if not access_token:
        # An oauth_2.1 connection with no access token is unauthorized (never
        # connected, or cleared after a terminal refresh failure) — surface a
        # re-auth signal so the chat path can prompt reconnection rather than
        # silently failing.
        raise MCPOAuthReauthRequired(
            "MCP connection is not authorized; please connect it."
        )
    expires_at = int(tokens.get("expires_at") or 0)
    if expires_at and expires_at > int(time.time()) + 300:
        return access_token

    # Near/at expiry — refresh under the per-connection lock.
    return await _refresh_personal_token(connection, stale_token=access_token)


async def _refresh_personal_token(
    connection: MCPConnectionWithSecrets, *, stale_token: Optional[str], force_refresh: bool = False
) -> Optional[str]:
    """Refresh the connection's OAuth access token, serialized per connection.

    Double-checked under the lock: if another task already produced a token
    different from ``stale_token`` (the one we hold / that just 401'd), we use
    that instead of refreshing again. On a terminal failure (invalid_grant) the
    stored tokens are cleared and ``MCPOAuthReauthRequired`` is raised so the UI
    can prompt re-authorization; transient failures propagate without wiping
    credentials so a retry can recover.

    ``force_refresh`` is set by the mid-session-401 hook: that caller KNOWS
    ``stale_token`` was just rejected by the server, so an unchanged token must
    be refreshed regardless of its still-valid clock expiry (the expiry-based
    short-circuit only makes sense at the resolve-time freshness boundary).
    """
    allow_localhost = bool((connection.policy or {}).get("allow_localhost_oauth"))
    lock = await _get_refresh_lock(connection.id)
    async with lock:
        # Re-read the freshest oauth state from the DB inside the lock.
        fresh = await MCPConnections.get_connection_by_id_and_user_id(
            connection.id, connection.user_id, include_secrets=True
        )
        oauth = (getattr(fresh, "oauth", None) or connection.oauth or {})
        tokens = oauth.get("tokens") or {}
        current = tokens.get("access_token")
        expires_at = int(tokens.get("expires_at") or 0)

        # Someone else already refreshed while we waited on the lock — either the
        # access token changed, or (only when this exact token did NOT just fail)
        # its expiry was pushed out of the refresh window, covering providers that
        # rotate only the refresh token. When force_refresh is set the caller is
        # asserting `stale_token` was server-rejected, so a clock-valid-but-same
        # token must still be refreshed rather than handed back.
        now = int(time.time())
        if current and (
            current != stale_token
            or (not force_refresh and expires_at and expires_at > now + 300)
        ):
            return current

        refresh = tokens.get("refresh_token")
        if not refresh:
            await MCPConnections.clear_oauth_tokens_by_id_and_user_id(
                connection.id, connection.user_id
            )
            raise MCPOAuthReauthRequired(
                "MCP connection has no refresh token; please reconnect."
            )

        try:
            refreshed = await refresh_token(oauth, allow_localhost=allow_localhost)
        except MCPOAuthError as exc:
            if is_terminal_token_error(exc):
                await MCPConnections.clear_oauth_tokens_by_id_and_user_id(
                    connection.id, connection.user_id
                )
                raise MCPOAuthReauthRequired(
                    "MCP connection authorization expired; please reconnect."
                ) from exc
            # Transient (5xx / network): keep tokens, let the caller surface it.
            raise

        new_access = refreshed.get("access_token")
        if not new_access:
            # A 2xx response without an access_token is malformed — but the
            # network refresh already hit the provider, which (for a rotating
            # provider like Notion) may have consumed/rotated the old refresh
            # token. Mirror the persist-failure branch below: clear the
            # possibly-dead tokens and force re-auth rather than leave a
            # rotated-away token in the DB to be replayed (which revokes the grant).
            await MCPConnections.clear_oauth_tokens_by_id_and_user_id(
                connection.id, connection.user_id
            )
            raise MCPOAuthReauthRequired(
                "MCP token refresh returned no access token; please reconnect."
            )

        new_tokens = {**tokens, **refreshed, "expires_at": token_expires_at(refreshed)}
        if not await MCPConnections.merge_oauth_tokens_by_id_and_user_id(
            connection.id, connection.user_id, new_tokens
        ):
            # The network refresh already ROTATED the refresh token at the provider
            # (the old one is now consumed/dead). If we couldn't persist the new
            # one, the DB still holds the rotated-away token; replaying it on the
            # next refresh makes a rotating provider (Notion) REVOKE the whole
            # grant. Clear the dead tokens and force re-auth rather than leave the
            # consumed token to be replayed.
            await MCPConnections.clear_oauth_tokens_by_id_and_user_id(
                connection.id, connection.user_id
            )
            raise MCPOAuthReauthRequired(
                "MCP token refresh could not be saved; please reconnect."
            )
        return new_access


async def _personal_oauth_refresh_cb(
    connection: MCPConnectionWithSecrets, stale_token: Optional[str]
) -> Optional[str]:
    """Mid-session 401 refresh hook for the transport auth flow (audit A6)."""
    try:
        fresh = await MCPConnections.get_connection_by_id_and_user_id(
            connection.id, connection.user_id, include_secrets=True
        )
    except Exception:
        return None
    if not fresh:
        return None
    try:
        return await _refresh_personal_token(
            fresh, stale_token=stale_token, force_refresh=True
        )
    except MCPOAuthError:
        return None


# Process-loader / code-injection env vars a user must NOT be able to set on a
# template stdio connection (they would execute arbitrary code in the template's
# process, bypassing the curated command). The template's own env is exempt.
_STDIO_TEMPLATE_ENV_DENYLIST = {
    "NODE_OPTIONS",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "LD_AUDIT",
    "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH",
    "DYLD_FRAMEWORK_PATH",
    "PYTHONSTARTUP",
    "PYTHONPATH",
    "PYTHONHOME",
    "BASH_ENV",
    "ENV",
    "PERL5OPT",
    "PERL5LIB",
    "RUBYOPT",
    "GIT_SSH_COMMAND",
}


async def build_personal_mcp_connect_kwargs(
    connection: MCPConnectionWithSecrets,
    *,
    user=None,
    metadata: Optional[dict] = None,
) -> dict:
    transport = connection.transport or "remote_http"
    if transport in {"remote_http", "remote_sse"} and connection.url:
        # SSRF guard: the connection URL is where we connect and ship the bearer
        # token, so block private/internal targets (admin allowlist applies).
        await validate_public_url(
            connection.url,
            allow_localhost=bool((connection.policy or {}).get("allow_localhost_oauth")),
        )
    bearer_token = await resolve_personal_bearer_token(connection)

    auth = None
    headers = {}
    # OAuth over streamable HTTP: use a refreshing auth flow so a mid-session 401
    # (access token expiring during a long agentic turn) triggers a serialized
    # refresh + retry instead of failing every remaining tool call (audit A6).
    use_refresh_auth = connection.auth_type == "oauth_2.1" and transport == "remote_http"
    if use_refresh_auth:
        from open_webui.utils.mcp.client import BearerRefreshAuth

        async def _refresh_cb(stale_token, _conn=connection):
            return await _personal_oauth_refresh_cb(_conn, stale_token)

        auth = BearerRefreshAuth(bearer_token, _refresh_cb)
    elif bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"

    if transport in {"remote_http", "remote_sse"}:
        # Use the same per-request header resolver as admin-managed MCP servers.
        # Besides static custom headers, this expands {{CHAT_ID}}, {{MESSAGE_ID}},
        # {{SESSION_ID}}, {{USER_ID}}, {{USER_NAME}}, and {{BROWSER_SESSION}}
        # from the live chat context. It also prevents custom values from
        # overriding transport/authentication headers.
        custom = resolve_tool_server_headers(
            {
                "headers": connection.headers,
                "info": {"id": connection.id},
            },
            user=user,
            metadata=metadata,
        )
        if custom:
            headers = {**headers, **custom}
        result = {"url": connection.url, "headers": headers or None, "transport": transport}
        if auth is not None:
            result["auth"] = auth
        # SSRF guard for the runtime httpx transport (the path that ships the
        # bearer token): the SDK's default client re-resolves the host and follows
        # redirects, so route it through a transport that re-validates every hop.
        result["httpx_client_factory"] = guarded_httpx_client_factory(
            allow_localhost=bool((connection.policy or {}).get("allow_localhost_oauth"))
        )
        return result

    home = mcp_home(connection.user_id, connection.id)
    template_id = (connection.meta or {}).get("template")
    template = STDIO_TEMPLATES.get(template_id or "") or {}
    user_env = {**(connection.env or {})}
    if template:
        # A template is an admin-curated preset: its command/args are
        # AUTHORITATIVE and the connection's stored command/args are ignored.
        # Otherwise a user holding only the default-on `mcp_stdio_templates`
        # permission could attach command=/bin/sh (or widen a sandbox arg) to a
        # template and get arbitrary host command execution. The user still
        # supplies env (template secrets), but process-loader / code-injection
        # vars are stripped so env can't smuggle execution either (e.g.
        # NODE_OPTIONS=--require evil.js, LD_PRELOAD=...).
        user_env = {
            k: v
            for k, v in user_env.items()
            if k.upper() not in _STDIO_TEMPLATE_ENV_DENYLIST
        }
        command = template.get("command")
        args = template.get("args") or []
    else:
        command = connection.command
        args = connection.args or []
    env = _substitute({**(template.get("env") or {}), **user_env}, home)
    args = _substitute(args, home)
    cwd = _substitute(connection.cwd, home) if connection.cwd else None

    if template.get("node_stdio_sanitize") or (connection.policy or {}).get("node_stdio_sanitize"):
        preload = _write_node_stdio_preload(home)
        current = env.get("NODE_OPTIONS", os.environ.get("NODE_OPTIONS", ""))
        env["NODE_OPTIONS"] = f"{current} --require {preload}".strip()

    env.setdefault("HOME", str(home))
    env.setdefault("USERPROFILE", str(home))
    return {
        "command": command,
        "args": args,
        "env": env,
        "cwd": cwd,
        "transport": "stdio",
    }


def resolve_personal_mcp_call_meta(
    connection: MCPConnectionWithSecrets,
    *,
    user=None,
    metadata: Optional[dict] = None,
) -> Optional[dict[str, str]]:
    """Resolve configured context entries into MCP ``tools/call`` `_meta`.

    Remote connections use the entries as real HTTP headers when their
    transport is opened. Stdio has no HTTP layer, so `_meta` is the
    protocol-native per-call carrier. Keeping this resolution at call time is
    important for persistent stdio processes shared across multiple chats.
    """
    if (connection.transport or "remote_http") != "stdio":
        return None
    resolved = resolve_tool_server_headers(
        {
            "headers": connection.headers,
            "info": {"id": connection.id},
        },
        user=user,
        metadata=metadata,
    )
    return resolved or None


def is_write_tool(tool_spec: dict) -> bool:
    annotations = tool_spec.get("annotations") or {}
    if annotations.get("readOnlyHint") is True:
        return False
    return True


def tool_filter_allows(tool_spec: dict, tool_filters: Optional[dict]) -> bool:
    """Include/exclude allow check shared by the per-user and admin MCP tool
    filters so both apply identical semantics.

    ``include`` is the allowlist. An ABSENT include (no filter, or filter without
    the key) imposes no allowlist — every tool passes, including ones the server
    adds later. A PRESENT include — even an empty list — is an explicit allowlist:
    only its members pass, so "disable all" (``include: []``) blocks every tool
    instead of inverting to "allow all". ``exclude`` is a denylist that always
    applies and wins over ``include``.
    """
    filters = tool_filters or {}
    include = filters.get("include")
    exclude = set(filters.get("exclude") or [])
    name = tool_spec.get("name")
    if include is not None and name not in set(include):
        return False
    if name in exclude:
        return False
    return True


def tool_allowed_by_policy(tool_spec: dict, connection: MCPConnectionWithSecrets) -> bool:
    if not tool_filter_allows(tool_spec, connection.tool_filters):
        return False
    name = tool_spec.get("name")
    # Many LOCAL stdio MCPs expose a connection/auth/status tool that is not
    # read-only in annotations but is required before any useful tools can run
    # (e.g. Outlook Assistant's device-code flow). Keep these available for stdio
    # servers the user explicitly configured — but NOT for remote servers, where
    # a malicious server could name a destructive tool "status" to slip past the
    # write gate (audit E4). Key strictly off transport: a remote connection
    # could otherwise carry a stray `command` and re-open the bypass.
    is_stdio = getattr(connection, "transport", None) == "stdio"
    if is_stdio and name in {"auth", "authenticate", "connection", "status"}:
        return True
    if is_write_tool(tool_spec) and not (connection.policy or {}).get("enable_write_tools", False):
        return False
    return True
