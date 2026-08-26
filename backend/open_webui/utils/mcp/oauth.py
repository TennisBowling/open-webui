import asyncio
import base64
import hashlib
import ipaddress
import logging
import os
import secrets
import socket
from typing import Any, Optional
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse

import aiohttp
import httpx
from aiohttp.abc import AbstractResolver

from open_webui.env import (
    AIOHTTP_CLIENT_SESSION_SSL,
    MCP_ALLOWED_PRIVATE_HOSTS,
    SRC_LOG_LEVELS,
)


log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["OAUTH"])


class MCPOAuthError(Exception):
    pass


class MCPOAuthHTTPError(MCPOAuthError):
    """An OAuth/discovery HTTP request returned a non-2xx response.

    Carries the status + (truncated) body so callers can distinguish a terminal
    token error (``invalid_grant`` / 400 / 401 from the token endpoint, meaning
    the grant is dead and the user must re-authenticate) from a transient one
    (5xx / 429 / network) that should be retried rather than wiping credentials.
    """

    def __init__(self, status: int, body: str, url: str):
        self.status = status
        self.body = body or ""
        self.url = url
        super().__init__(f"HTTP {status} from {url}: {self.body[:300]}")


class MCPOAuthReauthRequired(MCPOAuthError):
    """The connection's grant is no longer valid; the user must re-authorize."""


_TERMINAL_OAUTH_ERROR_CODES = {
    "invalid_grant",
    "invalid_client",
    "unauthorized_client",
    "invalid_request",
    "access_denied",
}


def is_terminal_token_error(exc: Exception) -> bool:
    """True when a refresh/token error means the grant is permanently dead.

    A terminal error must NOT be retried with the same refresh token (some
    providers, e.g. Notion, revoke the whole grant when a rotated-away refresh
    token is replayed) — the stored tokens should be cleared and re-auth forced.
    """
    if not isinstance(exc, MCPOAuthHTTPError):
        return False
    if exc.status in (400, 401):
        # The OAuth token endpoint reports terminal failures as 400/401 with an
        # ``error`` code in the JSON body (RFC 6749 §5.2).
        body = (exc.body or "").lower()
        if any(code in body for code in _TERMINAL_OAUTH_ERROR_CODES):
            return True
        # A bare 401 from the token endpoint is also terminal.
        return exc.status == 401
    return False


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


def _ip_is_blocked(ip) -> bool:
    # Unmap IPv4-mapped IPv6 (::ffff:a.b.c.d) so an embedded private/loopback v4
    # address is judged on its v4 properties, not the v6 wrapper's.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    # Deny-by-default: anything not globally routable (covers RFC 6598 CGNAT
    # 100.64.0.0/10, RFC 1918, etc.) plus the explicit special categories.
    return (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _host_is_loopback_name(host: str) -> bool:
    host = host.strip("[]").lower()
    return host in {"localhost", "localhost.localdomain"} or host.endswith(".local")


async def _resolve_host(host: str) -> list:
    """Resolve a hostname to all of its IP addresses.

    Resolving (rather than string-matching) is what closes the octal/decimal/hex
    integer-literal bypass (``getaddrinfo`` normalizes ``2130706433`` -> 127.0.0.1)
    and the DNS-name-resolving-to-private bypass.
    """
    bare = host.strip("[]")
    try:
        return [ipaddress.ip_address(bare)]
    except ValueError:
        pass
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(bare, None, proto=socket.IPPROTO_TCP)
    ips = []
    for info in infos:
        addr = info[4][0]
        try:
            ips.append(ipaddress.ip_address(addr.split("%")[0]))
        except ValueError:
            continue
    return ips


def _allowlisted(host: str, ips: list, allowlist) -> bool:
    host_l = host.strip("[]").lower()
    for entry in allowlist or []:
        entry = (entry or "").strip().lower()
        if not entry:
            continue
        if entry == host_l:
            return True
        try:
            net = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            continue
        if any(ip in net for ip in ips):
            return True
    return False


class _SSRFGuardedResolver(AbstractResolver):
    """An aiohttp resolver that drops blocked (private/internal) addresses.

    ``validate_public_url`` is a one-shot pre-flight check: it resolves the host
    once, but the actual outbound request resolves again inside aiohttp, and it
    re-resolves once more for every redirect hop. That leaves two gaps a guarded
    resolver closes by being consulted at the real connect time:

    * **DNS-rebind TOCTOU** — a low-TTL attacker domain can answer a public IP
      during validation and an internal IP during the connect. Here the IP that
      is checked IS the IP that is dialed, because we return only the addresses
      we just vetted.
    * **Redirect-to-internal SSRF (hostname hops)** — a public server can
      ``30x`` to an internal *hostname*; aiohttp re-resolves that hop through
      this resolver too, so it is blocked. NOTE: aiohttp short-circuits literal
      IP hosts and never calls a resolver for them, so a ``30x`` whose Location
      is a literal internal IP is NOT caught here — ``fetch_json`` closes that
      by following redirects manually and re-running ``validate_public_url`` on
      every hop (which validates literal IPs directly).

    Policy mirrors ``validate_public_url``: deny anything not globally routable,
    except loopback when ``allow_localhost`` is set, or a host/CIDR in the admin
    allowlist (``MCP_ALLOWED_PRIVATE_HOSTS`` plus per-call ``extra_allow``).
    """

    def __init__(
        self, *, allow_localhost: bool = False, extra_allow: Optional[list] = None
    ):
        self._allow_localhost = allow_localhost
        self._extra_allow = list(extra_allow or [])
        # Lazily built: ThreadedResolver grabs the running loop at construction,
        # so defer it to resolve()/close() which always run inside a loop.
        self._inner: Optional[aiohttp.ThreadedResolver] = None

    def _resolver(self) -> aiohttp.ThreadedResolver:
        if self._inner is None:
            self._inner = aiohttp.ThreadedResolver()
        return self._inner

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET
    ) -> list:
        infos = await self._resolver().resolve(host, port, family=family)
        allowlist = list(MCP_ALLOWED_PRIVATE_HOSTS) + self._extra_allow
        allowed = []
        for info in infos:
            try:
                ip = ipaddress.ip_address(info["host"])
            except ValueError:
                continue
            if _ip_is_blocked(ip):
                if self._allow_localhost and ip.is_loopback:
                    pass
                elif _allowlisted(host, [ip], allowlist):
                    pass
                else:
                    # Drop this address; if it is the only one we raise below.
                    continue
            allowed.append(info)
        if not allowed:
            raise OSError(
                f"SSRF guard: host '{host}' resolves only to blocked addresses"
            )
        return allowed

    async def close(self) -> None:
        if self._inner is not None:
            await self._inner.close()


def ssrf_guarded_connector(
    *, allow_localhost: bool = False, extra_allow: Optional[list] = None
) -> aiohttp.TCPConnector:
    """A TCPConnector whose resolver re-checks every connect against SSRF policy.

    The session owns and closes it (``connector_owner`` defaults to True), which
    in turn closes the resolver. ``ssl`` stays on the per-request call so this
    connector carries only the resolver.
    """
    return aiohttp.TCPConnector(
        resolver=_SSRFGuardedResolver(
            allow_localhost=allow_localhost, extra_allow=extra_allow
        )
    )


# Session teardown (the streamable-http DELETE issued during MCPClient.disconnect)
# must be bounded HERE, at the transport: disconnect() cannot wrap stack.aclose()
# in an anyio cancel scope (it would violate the transport's cancel-scope LIFO
# nesting), and the client-level read timeout is deliberately unbounded so long
# tool calls aren't cut off. MCP streamable-http uses DELETE exclusively for
# session termination, so a method-based bound is safe and precise.
MCP_TEARDOWN_TIMEOUT = float(os.environ.get("MCP_TEARDOWN_TIMEOUT", "15"))

# Kernel-level TCP keepalive for remote MCP connections. The read timeout is
# unbounded (see above), so a silently dropped peer (NAT/middlebox/CDN idle
# reap without FIN) would otherwise wedge a pending read forever. With these,
# a dead peer surfaces as a connection error after ~90s (60 + 3*10).
_MCP_SOCKET_OPTIONS = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
if hasattr(socket, "TCP_KEEPIDLE"):  # Linux; absent on macOS dev boxes
    _MCP_SOCKET_OPTIONS += [
        (socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60),
        (socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10),
        (socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3),
    ]


class _SSRFGuardedAsyncTransport(httpx.AsyncHTTPTransport):
    """An httpx transport that vets every request URL against the SSRF policy.

    The MCP runtime transport (``streamablehttp_client`` / ``sse_client``) is
    httpx-based and the SDK's default client hard-codes ``follow_redirects=True``
    while re-resolving the host itself, so the aiohttp guarded resolver never
    sees the connection that actually ships the user's bearer token. httpx calls
    the transport once per request AND once per redirect hop. For each, we
    validate ``request.url`` and then PIN the connection to one of the exact IPs
    that validation vetted (preserving the Host header + TLS SNI/cert via the
    ``sni_hostname`` extension). That makes the dialed address equal the validated
    address — closing the DNS-rebind TOCTOU and redirect-to-internal on the
    token-carrying path, to parity with the aiohttp ``_SSRFGuardedResolver``.
    """

    def __init__(
        self, *args, allow_localhost: bool = False, extra_allow: Optional[list] = None, **kwargs
    ):
        super().__init__(*args, **kwargs)
        self._allow_localhost = allow_localhost
        self._extra_allow = extra_allow

    async def handle_async_request(self, request):
        if request.method == "DELETE":
            # Session-termination request (see MCP_TEARDOWN_TIMEOUT above): a
            # hung remote must not stall disconnect()'s stack.aclose() — that
            # task has temporarily shed its pending cancellation and would
            # otherwise be wedged forever, leaking the connection.
            request.extensions = dict(request.extensions or {})
            request.extensions["timeout"] = {
                "connect": MCP_TEARDOWN_TIMEOUT,
                "read": MCP_TEARDOWN_TIMEOUT,
                "write": MCP_TEARDOWN_TIMEOUT,
                "pool": MCP_TEARDOWN_TIMEOUT,
            }
        host = request.url.host
        vetted = await validate_public_url(
            str(request.url),
            allow_localhost=self._allow_localhost,
            extra_allow=self._extra_allow,
        )
        # Pin to a vetted IP unless the host is already a literal IP (nothing to
        # rebind — validation already covered it). Dialing the IP while keeping
        # the Host header + TLS SNI on the hostname preserves cert verification.
        is_literal = False
        try:
            ipaddress.ip_address(host.strip("[]"))
            is_literal = True
        except ValueError:
            pass
        if vetted and not is_literal:
            port = request.url.port
            request.headers["Host"] = host if port is None else f"{host}:{port}"
            request.url = request.url.copy_with(host=vetted[0])
            request.extensions = dict(request.extensions or {})
            request.extensions["sni_hostname"] = host
        return await super().handle_async_request(request)


def guarded_httpx_client_factory(
    *, allow_localhost: bool = False, extra_allow: Optional[list] = None
):
    """Build an MCP ``httpx_client_factory`` that enforces the SSRF policy.

    Mirrors the SDK's ``create_mcp_http_client`` connection/write/pool timeout
    defaults but deliberately leaves the read timeout unbounded. Tool calls are
    already bounded at the Open WebUI MCP call layer when an admin timeout applies;
    exempt tools such as bash/web_search/web_fetch must not be cut off by a hidden
    lower-level HTTP read cap. The guarded transport still prevents the runtime MCP
    connection from being steered (by rebind or redirect) to an internal target.
    The signature matches ``McpHttpClientFactory``.
    """

    def _factory(headers=None, timeout=None, auth=None) -> httpx.AsyncClient:
        if timeout is None:
            timeout = httpx.Timeout(30.0, read=None)
        elif isinstance(timeout, httpx.Timeout):
            # The MCP SDK's deprecated streamablehttp_client/sse_client wrappers
            # pass httpx.Timeout(..., read=300) explicitly. Preserve the regular
            # operation budgets but remove that hidden long-poll/read cap.
            timeout = httpx.Timeout(
                timeout.connect,
                read=None,
                write=timeout.write,
                pool=timeout.pool,
            )
        else:
            timeout = httpx.Timeout(timeout, read=None)
        transport = _SSRFGuardedAsyncTransport(
            allow_localhost=allow_localhost,
            extra_allow=extra_allow,
            socket_options=_MCP_SOCKET_OPTIONS,
        )
        kwargs: dict = {
            "follow_redirects": True,
            "timeout": timeout,
            "transport": transport,
        }
        if headers is not None:
            kwargs["headers"] = headers
        if auth is not None:
            kwargs["auth"] = auth
        return httpx.AsyncClient(**kwargs)

    return _factory


async def validate_public_url(
    url: str,
    *,
    allow_localhost: bool = False,
    extra_allow: Optional[list] = None,
) -> list:
    """Block SSRF to private/internal targets; return the vetted IP strings.

    Resolves the host and rejects it if any resolved address is private/reserved/
    loopback/link-local/multicast, unless (a) ``allow_localhost`` is set and the
    address is loopback, or (b) the host/CIDR is in the admin allowlist
    (``MCP_ALLOWED_PRIVATE_HOSTS`` plus any per-call ``extra_allow``).

    Returns the resolved-and-vetted IP strings so a caller can dial exactly the
    address that was checked (closing the rebind window). Callers that only need
    the guard can ignore the return value.
    """
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        raise MCPOAuthError("MCP URL is missing a host")

    is_loopback_name = _host_is_loopback_name(host)
    if parsed.scheme != "https":
        if not (
            allow_localhost
            and parsed.scheme == "http"
            and (is_loopback_name or host.strip("[]") in {"127.0.0.1", "::1"})
        ):
            raise MCPOAuthError(
                "MCP URLs must use HTTPS unless localhost dev mode is enabled"
            )

    try:
        ips = await _resolve_host(host)
    except Exception as exc:
        raise MCPOAuthError(f"Could not resolve MCP host {host}: {exc}") from exc
    if not ips:
        raise MCPOAuthError(f"Could not resolve MCP host {host}")

    allowlist = list(MCP_ALLOWED_PRIVATE_HOSTS) + list(extra_allow or [])
    vetted = []
    for ip in ips:
        if not _ip_is_blocked(ip):
            vetted.append(ip)
            continue
        if allow_localhost and ip.is_loopback:
            vetted.append(ip)
            continue
        # Judge the allowlist against THIS single ip, not the whole resolved set.
        # Passing the full set let a host that resolves to one allowlisted-CIDR
        # address PLUS a forbidden one (e.g. 169.254.169.254) slip every IP
        # through — and the httpx transport then pins/dials the forbidden one.
        if _allowlisted(host, [ip], allowlist):
            vetted.append(ip)
            continue
        raise MCPOAuthError(
            f"MCP URL host '{host}' resolves to a blocked address ({ip})"
        )
    # Return only the individually-vetted IPs so a caller that pins to one can
    # never dial an address that was not itself checked.
    return [str(ip) for ip in vetted]


def protected_resource_urls(mcp_url: str) -> list[str]:
    parsed = urlparse(mcp_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    urls = []
    path = parsed.path.rstrip("/")
    if path:
        urls.append(urljoin(base, f"/.well-known/oauth-protected-resource{path}"))
    urls.append(urljoin(base, "/.well-known/oauth-protected-resource"))
    return urls


async def fetch_json(url: str, *, method: str = "GET", headers: Optional[dict] = None, data: Any = None, json_data: Any = None, allow_localhost: bool = False, extra_allow: Optional[list] = None) -> dict:
    # Redirects are followed MANUALLY, re-validating every hop, because aiohttp's
    # resolver short-circuits literal-IP hosts: a 30x whose Location is a literal
    # internal IP (http://169.254.169.254/, or the decimal form http://2130706433/)
    # would otherwise never reach the guarded resolver. validate_public_url checks
    # literal IPs directly, so per-hop validation closes that hole. POST (token /
    # DCR) never follows a redirect — a 307 must not replay the secret-bearing
    # body to the redirect target.
    method_u = (method or "GET").upper()
    timeout = aiohttp.ClientTimeout(total=20)
    current_url = url
    max_redirects = 5
    for _hop in range(max_redirects + 1):
        await validate_public_url(
            current_url, allow_localhost=allow_localhost, extra_allow=extra_allow
        )
        async with aiohttp.ClientSession(
            timeout=timeout,
            trust_env=True,
            connector=ssrf_guarded_connector(
                allow_localhost=allow_localhost, extra_allow=extra_allow
            ),
        ) as session:
            async with session.request(
                method_u,
                current_url,
                headers=headers,
                data=data,
                json=json_data,
                ssl=AIOHTTP_CLIENT_SESSION_SSL,
                allow_redirects=False,
            ) as response:
                if response.status in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if method_u == "GET" and location and _hop < max_redirects:
                        current_url = urljoin(current_url, location)
                        continue
                    # Non-GET, missing Location, or hop budget exhausted: surface
                    # as an error rather than silently following / hanging.
                    raise MCPOAuthHTTPError(
                        response.status, await response.text(), current_url
                    )
                text = await response.text()
                if response.status < 200 or response.status >= 300:
                    raise MCPOAuthHTTPError(response.status, text, current_url)
                try:
                    return await response.json()
                except Exception as exc:
                    raise MCPOAuthError(
                        f"Invalid JSON from {current_url}: {exc}"
                    ) from exc
    raise MCPOAuthError(f"Too many redirects fetching {url}")


def authorization_server_metadata_urls(issuer: str) -> list[str]:
    """Candidate AS-metadata URLs for an issuer (RFC 8414 + OIDC fallback).

    Tries oauth-authorization-server then openid-configuration, and for issuers
    that carry a path, the RFC 8414 path-inserted form as well as the appended
    form. Order matters: most-specific/standard first.
    """
    parsed = urlparse(issuer)
    base = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")
    urls: list[str] = []
    for wk in ("oauth-authorization-server", "openid-configuration"):
        if path:
            # RFC 8414 path-inserted form (preferred)...
            urls.append(urljoin(base, f"/.well-known/{wk}{path}"))
            # ...and the appended-after-path form (some servers use this; it was
            # the pre-D2 behavior, keep it so those don't regress).
            urls.append(urljoin(f"{base}{path}/", f".well-known/{wk}"))
        urls.append(urljoin(base, f"/.well-known/{wk}"))
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


async def discover_from_challenge(mcp_url: str, *, allow_localhost: bool = False) -> tuple[dict, dict]:
    await validate_public_url(mcp_url, allow_localhost=allow_localhost)
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
        async with aiohttp.ClientSession(
            timeout=timeout,
            trust_env=True,
            connector=ssrf_guarded_connector(allow_localhost=allow_localhost),
        ) as session:
            async with session.post(
                mcp_url,
                json=body,
                headers={"Content-Type": "application/json"},
                ssl=AIOHTTP_CLIENT_SESSION_SSL,
                allow_redirects=False,
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

    # D1: the discovered PRM must describe THIS server. A resource pointing at a
    # different host would bind the issued token to the wrong audience.
    declared_resource = resource_metadata.get("resource")
    if declared_resource and urlparse(str(declared_resource)).hostname != urlparse(mcp_url).hostname:
        raise MCPOAuthError(
            "Protected resource metadata 'resource' host does not match the MCP server"
        )

    # D2: try oauth-authorization-server, then the OIDC openid-configuration
    # fallback (and path-inserted variants for path-bearing issuers).
    auth_server = auth_servers[0]
    auth_metadata = None
    last_error = None
    for url in authorization_server_metadata_urls(str(auth_server)):
        try:
            auth_metadata = await fetch_json(url, allow_localhost=allow_localhost)
            break
        except Exception as exc:
            last_error = exc
    if not auth_metadata:
        raise MCPOAuthError(
            f"Failed to discover authorization server metadata: {last_error}"
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
    return await request_refresh_grant(
        token_endpoint,
        client_id=client_info.get("client_id"),
        refresh_token=refresh,
        headers=headers,
        extra_data=extra,
        resource=(oauth.get("resource_metadata") or {}).get("resource"),
        allow_localhost=allow_localhost,
    )


def token_expires_at(token_response: dict) -> int:
    import time

    expires_in = int(token_response.get("expires_in") or 3600)
    return int(time.time()) + expires_in


async def request_refresh_grant(
    token_endpoint: str,
    *,
    client_id: Optional[str],
    refresh_token: str,
    headers: Optional[dict] = None,
    extra_data: Optional[dict] = None,
    resource: Optional[str] = None,
    allow_localhost: bool = False,
    extra_allow: Optional[list] = None,
) -> dict:
    """Shared ``refresh_token`` grant request for BOTH the per-user and admin MCP
    OAuth paths, so their HTTP surface, redirect/SSRF hardening, and expires_at
    defaulting cannot drift — the admin path keeping its own copy is what produced
    the NULL-``expires_at`` crash and a divergent terminal-error list.

    POSTs through ``fetch_json`` (POST never follows redirects; each hop is URL-
    re-validated and IP-pinned). Raises ``MCPOAuthHTTPError`` on a non-2xx token
    response — classify it with ``is_terminal_token_error`` — and ``MCPOAuthError``
    on a transport/JSON failure. The returned token dict always carries a
    NOT-NULL-safe ``expires_at``.
    """
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        **(extra_data or {}),
    }
    if resource:
        data["resource"] = resource
    request_headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        **(headers or {}),
    }
    token = await fetch_json(
        token_endpoint,
        method="POST",
        headers=request_headers,
        data=urlencode(data),
        allow_localhost=allow_localhost,
        extra_allow=extra_allow,
    )
    if "expires_at" not in token:
        token["expires_at"] = token_expires_at(token)
    return token
