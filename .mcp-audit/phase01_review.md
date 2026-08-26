# Phase 0+1 MCP Fixes — Review

**Verdict:** 5 confirmed regressions, all severity **low** (no security, availability, or data-correctness impact; UX/efficiency/edge-case only).

## Confirmed regressions

### 1. Redundant second refresh when access_token is stable across rotation
- **Severity:** low
- **File:** `backend/open_webui/utils/mcp/connections.py:171-176`
- **What's wrong:** The under-lock double-check uses the `access_token` string as its only rotation sentinel (`if current and current != stale_token: return current`). If the OAuth provider rotates only the refresh_token and returns the *same* access_token value, the second waiter's `current != stale_token` is False, so it falls through and performs a second, redundant network refresh — defeating the single-refresh serialization the lock exists to enforce. The freshly-computed `expires_at` (line 172) is read but never used, so it cannot rescue this path. No grant death: the second refresh re-reads the already-rotated, valid refresh_token from `fresh` (line 178). Cost is one extra round-trip plus an extra rotation per concurrent waiter, only for providers that keep the access_token stable.
- **Fix:** Use a rotation-robust sentinel — e.g. short-circuit on `expires_at` advancing (`if current and expires_at > int(time.time()) + 300: return current`) or compare the refresh_token value, rather than the access_token alone.

### 2. Mid-session 401 refresh callback swallows `MCPOAuthReauthRequired`, losing the `needs_reauth` UX signal
- **Severity:** low
- **File:** `backend/open_webui/utils/mcp/connections.py:228-231`
- **What's wrong:** `_personal_oauth_refresh_cb` does `except MCPOAuthError: return None`. Because `MCPOAuthReauthRequired` subclasses `MCPOAuthError` (oauth.py:38), a terminal (invalid_grant) failure during a mid-session 401 retry is swallowed and returned as None. The `BearerRefreshAuth` flow then does not re-yield the request, the transport raises a generic connect/tool error, and `middleware.py:2409` computes `needs_reauth = isinstance(e, MCPOAuthReauthRequired)` → False. Tokens *are* cleared before the raise, so correctness/security is fine and the next verify/tools call re-prompts; only the in-turn re-auth banner is missed when the grant dies via a transport 401 rather than at connect time. (The connect-time path correctly propagates the typed signal to routers/mcp.py:270/291.)
- **Fix:** Distinguish `MCPOAuthReauthRequired` from generic `MCPOAuthError` in this except so the typed re-auth signal propagates, or have the mid-session flow/middleware detect that tokens were cleared and set `needs_reauth` on that path too.

### 3. Per-connection refresh-lock registry grows without eviction
- **Severity:** low
- **File:** `backend/open_webui/utils/mcp/connections.py:32-42`
- **What's wrong:** `_refresh_locks` is a plain module-level `dict[str, asyncio.Lock]`. `_get_refresh_lock` inserts one lock per distinct connection id ever refreshed and nothing ever removes entries — `delete_connection_by_id_and_user_id` (models/mcp.py:348-358) issues only a SQL DELETE and never touches the in-process map. Growth is bounded by the count of distinct connection ids the process has ever serviced, not by currently-live connections. Each entry is a tiny bare lock, so the leak is slow and bounded; relevant only for very long-lived processes with high churn of distinct OAuth connections. No correctness or security impact.
- **Fix:** Optional — evict the lock entry on connection deletion, or use a bounded LRU / `WeakValueDictionary`. Not required for the single-worker runtime.

### 4. `OAuthClientManager._refresh_token` lost its outer try/except; a None from `update_session_by_id` now raises `AttributeError`
- **Severity:** low
- **File:** `backend/open_webui/utils/oauth.py:501-521`
- **What's wrong:** The rewrite dropped the HEAD version's outer `try/except Exception: return None`. After a successful refresh it does `session = await OAuthSessions.update_session_by_id(...)` then immediately `log.info(... session.id ...)` and `return session.token` with no None-guard. `update_session_by_id` (oauth_sessions.py:182-185) returns None on a DB error or when the row no longer exists (concurrent delete), so those dereferences raise `AttributeError`. The error is still contained by `get_oauth_token`'s outer `except Exception: return None` (497-499), so the externally-observable result is unchanged — but the terminal-delete branch is skipped and the freshly-refreshed token is discarded without persisting, forcing a re-refresh next call. Rare race/DB-error path only; no security or availability regression.
- **Fix:** Guard the update result: `updated = await OAuthSessions.update_session_by_id(...); if updated: return updated.token; return refreshed_token` (or restore a narrow try/except) so a None does not raise and the refreshed token is still returned.

### 5. Reauth re-read can pass None to `_start_oauth`, raising `AttributeError` (500) on mid-request deletion
- **Severity:** low
- **File:** `backend/open_webui/routers/mcp.py:270-275, 291-295`
- **What's wrong:** When `_list_specs` raises `MCPOAuthReauthRequired`, both `get_mcp_connection_tools` and `verify_mcp_connection` re-read the connection and splat it straight into `await _start_oauth(request, connection)` without a None-guard (unlike the guarded first reads at 266-267 / 285-286). `get_connection_by_id_and_user_id` returns None if the row was deleted between the two reads; `_start_oauth` then dereferences `connection.transport` (line 110) and raises `AttributeError`. Critically, that raise occurs *inside* the `except MCPOAuthReauthRequired:` handler, so the sibling `except Exception` does not catch it and it surfaces to FastAPI as an unhandled 500. Narrow delete-mid-request race only — opaque 500 instead of a clean 404, no data corruption or chat-turn crash.
- **Fix:** After the reauth re-read, re-check for None before calling `_start_oauth`, e.g. `if not connection: raise HTTPException(status_code=404, detail=ERROR_MESSAGES.NOT_FOUND)` (or return the reauth-required envelope).

## Uncertain

None — there were no uncertain findings to evaluate.
