import base64
import hashlib
import ipaddress
import logging
import secrets
from typing import Any, Optional
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse

import aiohttp

from open_webui.env import AIOHTTP_CLIENT_SESSION_SSL, SRC_LOG_LEVELS


log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["OAUTH"])


class MCPOAuthError(Exception):
    pass


def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def create_pkce() -> tuple[str, str]:
    verifier = _base64url(secrets.token_bytes(32))
    challenge = _base64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def create_state() -> str:
    return _base64url(secrets.token_bytes(32))


def parse_www_authenticate(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    value = value.strip()
    if value.lower().startswith("bearer"):
        value = value[6:].strip()

    parts: list[str] = []
    current = []
    in_quotes = False
    escape = False
    for ch in value:
        if escape:
            current.append(ch)
            escape = False
            continue
        if ch == "\\" and in_quotes:
            escape = True
            continue
        if ch == '"':
            in_quotes = not in_quotes
            continue
        if ch == "," and not in_quotes:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current).strip())

    result = {}
    for part in parts:
        if "=" not in part:
            continue
        key, val = part.split("=", 1)
        result[key.strip()] = val.strip().strip('"')
    return result


def _host_is_private(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
    except ValueError:
        return host in {"localhost", "localhost.localdomain"} or host.endswith(".local")


def validate_public_url(url: str, *, allow_localhost: bool = False) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        if not (allow_localhost and parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
            raise MCPOAuthError("MCP OAuth URLs must use HTTPS unless localhost dev mode is enabled")
    if not parsed.hostname:
        raise MCPOAuthError("MCP OAuth URL is missing a host")
    if _host_is_private(parsed.hostname) and not allow_localhost:
        raise MCPOAuthError("MCP OAuth metadata URL points to a private or local host")


def protected_resource_urls(mcp_url: str) -> list[str]:
    parsed = urlparse(mcp_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    urls = []
    path = parsed.path.rstrip("/")
    if path:
        urls.append(urljoin(base, f"/.well-known/oauth-protected-resource{path}"))
    urls.append(urljoin(base, "/.well-known/oauth-protected-resource"))
    return urls


async def fetch_json(url: str, *, method: str = "GET", headers: Optional[dict] = None, data: Any = None, json_data: Any = None, allow_localhost: bool = False) -> dict:
    validate_public_url(url, allow_localhost=allow_localhost)
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
        async with session.request(
            method,
            url,
            headers=headers,
            data=data,
            json=json_data,
            ssl=AIOHTTP_CLIENT_SESSION_SSL,
        ) as response:
            text = await response.text()
            if response.status < 200 or response.status >= 300:
                raise MCPOAuthError(f"HTTP {response.status} from {url}: {text[:300]}")
            try:
                return await response.json()
            except Exception as exc:
                raise MCPOAuthError(f"Invalid JSON from {url}: {exc}") from exc


async def discover_from_challenge(mcp_url: str, *, allow_localhost: bool = False) -> tuple[dict, dict]:
    validate_public_url(mcp_url, allow_localhost=allow_localhost)
    resource_metadata_url = None
    timeout = aiohttp.ClientTimeout(total=10)
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "open-webui", "version": "0"},
        },
    }
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.post(
                mcp_url,
                json=body,
                headers={"Content-Type": "application/json"},
                ssl=AIOHTTP_CLIENT_SESSION_SSL,
            ) as response:
                challenge = parse_www_authenticate(response.headers.get("WWW-Authenticate"))
                resource_metadata_url = challenge.get("resource_metadata")
    except Exception:
        log.debug("MCP OAuth challenge probe failed", exc_info=True)

    if resource_metadata_url:
        resource_metadata = await fetch_json(resource_metadata_url, allow_localhost=allow_localhost)
    else:
        last_error = None
        resource_metadata = None
        for url in protected_resource_urls(mcp_url):
            try:
                resource_metadata = await fetch_json(url, allow_localhost=allow_localhost)
                break
            except Exception as exc:
                last_error = exc
        if not resource_metadata:
            raise MCPOAuthError(f"Failed to discover protected resource metadata: {last_error}")

    auth_servers = resource_metadata.get("authorization_servers") or []
    if not auth_servers:
        raise MCPOAuthError("Protected resource metadata did not include authorization_servers")

    auth_server = auth_servers[0]
    auth_metadata = await fetch_json(
        urljoin(str(auth_server).rstrip("/") + "/", ".well-known/oauth-authorization-server"),
        allow_localhost=allow_localhost,
    )
    return resource_metadata, auth_metadata


async def register_client(auth_metadata: dict, redirect_uri: str, *, client_name: str = "Open WebUI", allow_localhost: bool = False) -> dict:
    endpoint = auth_metadata.get("registration_endpoint")
    if not endpoint:
        raise MCPOAuthError("Authorization server does not support dynamic client registration")
    return await fetch_json(
        endpoint,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        json_data={
            "client_name": client_name,
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
        allow_localhost=allow_localhost,
    )


def build_authorization_url(auth_metadata: dict, client_info: dict, resource_metadata: dict, redirect_uri: str, state: str, code_challenge: str) -> str:
    endpoint = auth_metadata.get("authorization_endpoint")
    if not endpoint:
        raise MCPOAuthError("Authorization server metadata missing authorization_endpoint")
    scopes = resource_metadata.get("scopes_supported") or auth_metadata.get("scopes_supported") or []
    params = {
        "response_type": "code",
        "client_id": client_info.get("client_id"),
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if scopes:
        params["scope"] = " ".join(scopes)
    if resource_metadata.get("resource"):
        params["resource"] = resource_metadata["resource"]
    return f"{endpoint}?{urlencode(params)}"


def _token_auth(auth_metadata: dict, client_info: dict) -> tuple[dict, dict]:
    methods = auth_metadata.get("token_endpoint_auth_methods_supported") or []
    client_secret = client_info.get("client_secret")
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    extra = {}
    if client_secret and "client_secret_post" in methods:
        extra["client_secret"] = client_secret
    elif client_secret and "client_secret_basic" in methods:
        raw = f"{client_info.get('client_id')}:{client_secret}".encode("utf-8")
        headers["Authorization"] = f"Basic {base64.b64encode(raw).decode('ascii')}"
    return headers, extra


async def exchange_code(oauth: dict, code: str, redirect_uri: str, *, allow_localhost: bool = False) -> dict:
    auth_metadata = oauth.get("auth_metadata") or {}
    client_info = oauth.get("client_info") or {}
    resource_metadata = oauth.get("resource_metadata") or {}
    token_endpoint = auth_metadata.get("token_endpoint")
    if not token_endpoint:
        raise MCPOAuthError("Authorization server metadata missing token_endpoint")
    headers, extra = _token_auth(auth_metadata, client_info)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_info.get("client_id"),
        "redirect_uri": redirect_uri,
        "code_verifier": oauth.get("code_verifier"),
        **extra,
    }
    if resource_metadata.get("resource"):
        data["resource"] = resource_metadata["resource"]
    return await fetch_json(
        token_endpoint,
        method="POST",
        headers=headers,
        data=urlencode(data),
        allow_localhost=allow_localhost,
    )


async def refresh_token(oauth: dict, *, allow_localhost: bool = False) -> dict:
    auth_metadata = oauth.get("auth_metadata") or {}
    client_info = oauth.get("client_info") or {}
    tokens = oauth.get("tokens") or {}
    token_endpoint = auth_metadata.get("token_endpoint")
    refresh = tokens.get("refresh_token")
    if not token_endpoint or not refresh:
        raise MCPOAuthError("No refresh token available")
    headers, extra = _token_auth(auth_metadata, client_info)
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": client_info.get("client_id"),
        **extra,
    }
    resource = (oauth.get("resource_metadata") or {}).get("resource")
    if resource:
        data["resource"] = resource
    return await fetch_json(
        token_endpoint,
        method="POST",
        headers=headers,
        data=urlencode(data),
        allow_localhost=allow_localhost,
    )


def token_expires_at(token_response: dict) -> int:
    import time

    expires_in = int(token_response.get("expires_in") or 3600)
    return int(time.time()) + expires_in
