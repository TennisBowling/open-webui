# Phase 2 MCP Security Fix Review

**Verdict:** 4 confirmed findings — worst severity **high** (1 high, 3 low).

## Confirmed

### 1. [HIGH] E4 write-tool whitelist bypassable via remote connection with non-empty `command`
- **File:** `backend/open_webui/utils/mcp/connections.py:358-362`
- **Issue:** `tool_allowed_by_policy` detects stdio with `is_stdio = (transport == "stdio") or bool(connection.command)`. A remote connection (`remote_http`/`remote_sse`) can carry a non-empty `command`, so the second OR-clause whitelists tools named `auth`/`authenticate`/`connection`/`status` past the write gate. The remote-vs-stdio routing in `build_personal_mcp_connect_kwargs` (lines 262-308) branches only on `transport`+`url` and ignores `command`, so the server is contacted remotely (shipping the user's bearer/OAuth token) while the gate believes it is stdio. Two unblocked paths set `command` on a remote row: create (`_validate_form` in `routers/mcp.py:82-114` never rejects `command` on remote; `insert_new_connection` persists it via `model_dump(exclude={"key","headers","env"})`) and update (a stdio→remote transport switch leaves the stale `command` because `update_connection_by_id_and_user_id` writes only supplied fields). The gate is the execution-time filter (`utils/middleware.py:2342`), so this affects real tool calls, not just spec listing.
- **Fix:** Tie the stdio whitelist to transport only: `is_stdio = (getattr(connection, "transport", None) == "stdio")`. Legit stdio rows always have `transport=='stdio'`, so the `command` OR-clause adds no coverage and is the bypass vector. Optionally also reject/clear `command` for remote transports in `_validate_form`/update.

### 2. [LOW] CGNAT range 100.64.0.0/10 (RFC 6598) not blocked by `_ip_is_blocked`
- **File:** `backend/open_webui/utils/mcp/oauth.py:130-138`
- **Issue:** `_ip_is_blocked` enumerates explicit categories (`is_private`/`is_loopback`/`is_link_local`/`is_reserved`/`is_multicast`/`is_unspecified`) instead of a deny-by-default `not is_global` test. On the supported runtimes (Python 3.11–3.12; CGNAT not added to `is_private` until 3.13+), the entire 100.64.0.0/10 shared-address block reports all six predicates False, so it is not blocked. A user-supplied MCP/OAuth URL resolving into that block passes the SSRF guard and the bearer token is shipped there. The classic targets (169.254.169.254 metadata, 10/8, 192.168/16, 127.0.0.1, ::1, fc00::/7, fe80::/10, integer-literal, IPv4-mapped) are all correctly blocked.
- **Fix:** Add 100.64.0.0/10 (and IPv6 special ranges if desired) as a blocked category, or switch to `not ip.is_global` (keeping the `allow_localhost`/allowlist escape hatches) after confirming `is_global` semantics across supported Python versions.

### 3. [LOW] (Duplicate of #2, `ssrf-apply` dimension) RFC 6598 CGNAT gap in the E1 per-connection SSRF guard
- **File:** `backend/open_webui/utils/mcp/oauth.py:130-138`
- **Issue:** Same root cause as #2, observed through `validate_public_url` (the E1 guard applied to every per-user remote MCP connection URL via `build_personal_mcp_connect_kwargs`, `connections.py:266`). `MCP_ALLOWED_PRIVATE_HOSTS` defaults empty (`env.py:853-857`), so nothing masks the gap. A per-user remote connection whose hostname resolves into 100.64.0.0/10 is treated as public and the per-user bearer/OAuth token is sent to that internal-range address. Reachable only where CGNAT routes internally (common on some clouds/k8s).
- **Fix:** Same as #2 — block RFC 6598 in `_ip_is_blocked` or use `not ip.is_global`; admins can permit specific hosts via `MCP_ALLOWED_PRIVATE_HOSTS`.

### 4. [LOW] Validate-then-connect DNS-rebinding TOCTOU: validated IPs are not pinned for the actual connect
- **File:** `backend/open_webui/utils/mcp/oauth.py:187-233`
- **Issue:** `validate_public_url` resolves the host and checks the IPs but returns `None` — it never pins/returns a vetted address. `build_personal_mcp_connect_kwargs` then connects with the original hostname URL (`connections.py:305`), and `MCPClient.connect` (`utils/mcp/client.py`) re-resolves DNS independently at connect time via the underlying HTTP client. No IP-pinning resolver/connector exists anywhere in `utils/mcp/`. An attacker controlling authoritative DNS (short TTL) can answer with a public IP during validation and a private/internal IP (e.g. 169.254.169.254) at connect time, landing the user's token on an internal target. This is an inherent residual limitation of the net-new validate-then-connect design (introduced in the same Phase 2 commit), not a regression.
- **Fix:** Have `validate_public_url` return the vetted IP(s) and connect to the IP directly (preserving SNI/Host header), or use an aiohttp connector whose resolver re-checks each resolved IP against `_ip_is_blocked` at socket-connect time. If deferred, document the residual risk for non-allowlisted hosts.

## Uncertain

None.
