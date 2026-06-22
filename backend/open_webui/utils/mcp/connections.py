import os
import stat
import time
from pathlib import Path
from typing import Optional

from open_webui.env import DATA_DIR
from open_webui.models.mcp import MCPConnectionWithSecrets, MCPConnections
from open_webui.utils.mcp.oauth import refresh_token, token_expires_at
from open_webui.utils.tools import resolve_tool_server_headers


PERSONAL_MCP_PREFIX = "user:mcp:"


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
    if not tokens.get("access_token"):
        return None
    expires_at = int(tokens.get("expires_at") or 0)
    if expires_at and expires_at > int(time.time()) + 300:
        return tokens.get("access_token")

    refreshed = await refresh_token(
        oauth,
        allow_localhost=bool((connection.policy or {}).get("allow_localhost_oauth")),
    )
    tokens = {
        **tokens,
        **refreshed,
        "expires_at": token_expires_at(refreshed),
    }
    oauth["tokens"] = tokens
    await MCPConnections.update_oauth_by_id_and_user_id(connection.id, connection.user_id, oauth)
    return tokens.get("access_token")


async def build_personal_mcp_connect_kwargs(
    connection: MCPConnectionWithSecrets,
    *,
    user=None,
    metadata: Optional[dict] = None,
) -> dict:
    transport = connection.transport or "remote_http"
    bearer_token = await resolve_personal_bearer_token(connection)
    headers = {}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    for header in connection.headers or []:
        key = str(header.get("key", "")).strip()
        value = str(header.get("value", ""))
        if key and key.lower() not in {"authorization", "content-type", "accept", "cookie"}:
            headers[key] = value

    if transport in {"remote_http", "remote_sse"}:
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
        return {"url": connection.url, "headers": headers or None, "transport": transport}

    home = mcp_home(connection.user_id, connection.id)
    env = {**(connection.env or {})}
    template_id = (connection.meta or {}).get("template")
    template = STDIO_TEMPLATES.get(template_id or "") or {}
    env = {**(template.get("env") or {}), **env}
    env = _substitute(env, home)
    args = _substitute(connection.args or template.get("args") or [], home)
    command = connection.command or template.get("command")
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


def is_write_tool(tool_spec: dict) -> bool:
    annotations = tool_spec.get("annotations") or {}
    if annotations.get("readOnlyHint") is True:
        return False
    return True


def tool_allowed_by_policy(tool_spec: dict, connection: MCPConnectionWithSecrets) -> bool:
    filters = connection.tool_filters or {}
    include = set(filters.get("include") or [])
    exclude = set(filters.get("exclude") or [])
    name = tool_spec.get("name")
    if include and name not in include:
        return False
    if name in exclude:
        return False
    # Many local stdio MCPs expose a connection/auth/status tool that is not
    # read-only in annotations but is required before any useful tools can run
    # (for example Outlook Assistant's device-code flow). Keep these available
    # while still hiding actual write/destructive domain tools by default.
    if name in {"auth", "authenticate", "connection", "status"}:
        return True
    if is_write_tool(tool_spec) and not (connection.policy or {}).get("enable_write_tools", False):
        return False
    return True
