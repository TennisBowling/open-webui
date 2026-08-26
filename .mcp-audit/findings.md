# MCP Audit — Confirmed Findings (open-webui)

Adversarial audit of MCP support, focus on **per-user MCP** + **Notion hosted MCP** (OAuth).
8 dimensions × find→independent-skeptic-verify. **47 confirmed, 2 refuted.** Raw data: `findings_raw.json`.

Severity = post-verification "corrected_severity". The dominant theme: **OAuth token-refresh is unsafe**
(the rotation race surfaced independently from 8 finder angles), and a **data-model bug blocks per-user MCP
persistence on Postgres entirely**.

Refuted (not bugs): `notion-oauth-flow-3` (resource IS sent on refresh), `middleware-integration-6`
(SDK guarantees `content` is a list).

---

## Why "Notion just cannot be used" — the smoking guns

1. **[C1 / CRITICAL] Per-user MCP connections can't be written on Postgres.** Migration
   `2b7c9d4e8f01` creates `args/policy/tool_filters/meta` as `sa.Text()`, but `models/mcp.py` maps them to
   `JSONField` (`impl=JSONB`). On asyncpg the bind renders `$N::JSONB` into a `text` column → INSERT/UPDATE
   raises → swallowed → `insert_new_connection` returns `None` → "Failed to create MCP connection".
   `insert_new_connection` always sets these to `[]`/`{}`, so **every** per-user connection create fails.
   Masked by tests because the dev DB is SQLite (no bind cast). *Verified by compiling the INSERT.*
2. **[A1 / HIGH] OAuth refresh isn't serialized.** `resolve_personal_bearer_token` (connections.py:100-127)
   does an unlocked read-modify-write of the rotating refresh token. Two concurrent same-user requests near
   expiry both POST the same refresh token; Notion rotates and **revokes the whole grant** on reuse → Notion
   dies after working briefly. No lock anywhere.
3. **[A3 / HIGH] `invalid_grant` is not terminal.** On a failed refresh, tokens are never cleared, the UI
   still shows `authenticated=true`, and every chat re-POSTs the dead refresh token (compounding revocation).
   No re-auth signal.
4. **[B1 / CRITICAL] Admin/global MCP OAuth can never refresh.** `OAuthClientManager.get_server_metadata_url`
   reads `_server_metadata_url` off the wrapper **dict** instead of the authlib client → always `None` →
   refresh always fails → `get_oauth_token` **deletes the session** → admin must re-auth ~hourly.

---

## Theme A — OAuth token refresh is unsafe (per-user)  *(Notion-blocking)*

| ID | Sev | File:lines | Issue |
|----|-----|-----------|-------|
| A1 | HIGH | connections.py:100-127 | No per-connection refresh serialization → rotated-token reuse revokes grant. (also: notion-oauth-flow-1, token-refresh-concurrency-1, security-4, middleware-integration-1, data-model-storage-3, transport-client-7) |
| A2 | HIGH | models/mcp.py:251-278; mcp.py:288-313 | Full-blob read-modify-write of `oauth`, no row lock/CAS → lost updates drop newest rotated refresh token. (token-refresh-concurrency-2, data-model-storage-4) |
| A3 | HIGH | connections.py:116-127; middleware.py:2383-2410 | `invalid_grant`/refresh failure not terminal; tokens never cleared; surfaced as transient; `authenticated` stays true. (notion-oauth-flow-2, security-5, token-refresh-concurrency-3) |
| A4 | MED | connections.py:126-127 | Persist result unchecked; a swallowed DB write loses the rotated token while provider retired the old one. (middleware-integration-2) |
| A5 | LOW | connections.py:116-127 | Refresh response not validated; a 200 w/ partial body stamps a fresh `expires_at` and suppresses re-refresh ~1h. (token-refresh-concurrency-5) |
| A6 | HIGH | client.py:110-114,55-56 | Bearer frozen into static headers at connect; SDK `auth=` unused → long turns 401 mid-run with no refresh. (transport-client-1, token-refresh-concurrency-4) |

## Theme B — Admin/global MCP OAuth path  *(Notion-blocking + divergent)*

| ID | Sev | File:lines | Issue |
|----|-----|-----------|-------|
| B1 | CRIT | oauth.py:384-392,425-433 | `get_server_metadata_url` reads off wrapper dict → refresh always fails → session deleted. (admin-global-mcp-1) |
| B2 | HIGH | oauth.py:440-467,469-554 | Global refresh also unserialized (same rotation race; per-user-scoped blast radius). (admin-global-mcp-2) |
| B3 | MED | oauth.py:203-224 | Global path: no RFC9728 PRM discovery, never sends RFC8707 `resource` param. (admin-global-mcp-3) |
| B4 | MED | oauth.py:252-292,493-524 | Global discovery/DCR/token fetches have no SSRF guard (per-user path has one). (admin-global-mcp-4) |
| B5 | MED | oauth.py:425-433 | Session deleted on ANY refresh failure (transient 5xx/429/network), forcing avoidable re-auth. (admin-global-mcp-5) |
| B6 | LOW | oauth.py (two managers) | Two divergent copy-pasted OAuth implementations; bug fixed in one stays in the other. (admin-global-mcp-7) |

## Theme C — Data model / migrations  *(C1 Notion-blocking)*

| ID | Sev | File:lines | Issue |
|----|-----|-----------|-------|
| C1 | CRIT | migration 2b7c9d4e8f01:35,42-44 vs models/mcp.py:60,67-69 | JSON columns created `sa.Text()` but model is `JSONField(JSONB)` → write fails on Postgres → per-user MCP persistence silently broken. (data-model-storage-1) |
| C2 | MED | internal/migrations/019 vs alembic | Dead peewee migration duplicates the table w/ different indexes; dev DB built from it hides C1 in tests. (data-model-storage-2) |
| C3 | LOW | models/mcp.py:177-210 | insert id collision check is check-then-act TOCTOU; fallback reuses second-granularity timestamp. (data-model-storage-5) |

## Theme D — Per-user OAuth discovery correctness

| ID | Sev | File:lines | Issue |
|----|-----|-----------|-------|
| D1 | MED | oauth.py:140-166 | Challenge probe swallows all errors; fallback can pick root PRM (`resource=https://mcp.notion.com`, wrong audience) on transient failure. (notion-oauth-flow-5) |
| D2 | LOW | oauth.py:171-176 | No OIDC `/.well-known/openid-configuration` fallback, no RFC8414 path-insertion for AS metadata. (notion-oauth-flow-6) |
| D3 | LOW | mcp.py:118-138 | `disconnect` wipes `client_info`; every reconnect re-runs DCR, orphaning Notion client registrations. (notion-oauth-flow-4) |

## Theme E — Security (SSRF / policy / authz)

| ID | Sev | File:lines | Issue |
|----|-----|-----------|-------|
| E1 | HIGH | connections.py:130-158; client.py:110-114 | Runtime/verify MCP **connection URL** never SSRF-validated (only OAuth-discovery URLs are). Bearer + static headers shipped to arbitrary internal hosts. (security-1, transport-client-6) |
| E2 | HIGH | mcp.py + models/mcp.py | `policy.allow_localhost_oauth` is a free-form key any user can self-set → self-grants the SSRF escape hatch. (security-2) |
| E3 | HIGH | oauth.py:75-91 | `_host_is_private` is string-only: octal/decimal IP literals + DNS names resolving private bypass it (DNS-rebind TOCTOU). (security-3) |
| E4 | MED | connections.py:193-210 | Write-tool gate bypassed by name whitelist `{auth,authenticate,connection,status}` before the readOnly check. (middleware-integration-4) |
| E5 | LOW | mcp.py:288-317 | Callback is id-only lookup; `None==None` state window; state not session-bound. (security-6) |
| E6 | LOW | mcp.py:210-221 | Editing `url` after OAuth doesn't clear tokens → audience-bound token sent to new host. (security-7) |

## Theme F — Transport / robustness

| ID | Sev | File:lines | Issue |
|----|-----|-----------|-------|
| F1 | HIGH | client.py:147-186; middleware serial loop | `list_tool_specs` bounded by 900s tool-call timeout; serial connects → one hung server blocks chat setup minutes→15min. (middleware-integration-3) |
| F2 | MED | client.py:16,133-134 | `MCP_INIT_TIMEOUT=10s` too tight for cold remote init (SDK allows 30s); no retry → cold Notion spuriously fails. (transport-client-3) |
| F3 | MED | client.py:100,136-145,236-264 | connect() failure/cancel path bypasses disconnect()'s cancel-scope protection (plain `async with` teardown). (transport-client-2) |
| F4 | LOW | client.py:258-260 | `disconnect()` not time-bounded; session-end DELETE can block loop ~30s. (transport-client-4) |
| F5 | LOW | client.py:199-206 | `call_tool` drops `structuredContent` (2025-06-18 structured output). (transport-client-5) |
| F6 | LOW | middleware.py:2298-2337 | Duplicate personal `tool_id` overwrites `mcp_clients[key]`, leaking the first client. (middleware-integration-5) |

## Theme G — Frontend OAuth UX & governance

| ID | Sev | File:lines | Issue |
|----|-----|-----------|-------|
| G1 | MED | IntegrationsMenu.svelte:494-521; PersonalMCPConnections.svelte:127,139; mcp.py:317 | OAuth uses `_self`; callback returns to root `/` → destroys originating chat; tool-enable intent lost. (frontend-oauth-ux-1, -6) |
| G2 | MED | IntegrationsMenu.svelte:507-521 | OAuth-start has no try/catch → silent failure after "Opening sign-in" toast. (frontend-oauth-ux-2) |
| G3 | MED | PersonalMCPConnections.svelte:123-143 | "Verify" on unauthenticated conn silently redirects to OAuth, no explanation; error renders `[object Object]`. (frontend-oauth-ux-4) |
| G4 | LOW | (app)/+page.svelte:8-12; mcp.py:296,316 | OAuth error surfaced as raw class name/code; no needs-reauth mapping. (frontend-oauth-ux-3) |
| G5 | MED | routers/mcp.py | No admin visibility/audit/revocation over per-user MCP connections (governance/kill-switch gap). (admin-global-mcp-6) |
| G6 | LOW | stores/index.ts:199-200 | `mcpConnections`/`mcpConnectionsLoaded` stores declared but unwired. (frontend-oauth-ux-5) |

---

## Suggested fix ordering (dependency-aware)

**Phase 0 — Unblock (make Notion connectable at all):**
1. C1 — fix migration column types to `JSON()`/JSONB (+ data migration to `ALTER ... USING ::jsonb`); regenerate test fixture from Alembic. *(without this, nothing else matters)*
2. C2 — delete dead peewee `019`; ensure tests run the Alembic schema.

**Phase 1 — Make OAuth correct & durable (Notion stays alive):**
3. A1+A2 — per-connection refresh serialization (asyncio.Lock keyed by id + DB row lock / re-read-under-lock + atomic persist of rotated token).
4. A3 (+A4,A5) — treat `invalid_grant`/4xx as terminal: clear tokens, flip `authenticated`, surface needs-reauth; only retry transient.
5. B1 — fix `get_server_metadata_url` to read off the authlib client; B5 — only delete session on terminal errors; B2 — same serialization for the global path.
6. A6 — refresh-on-401 mid-session (wire SDK `auth=` provider or app-level reconnect).

**Phase 2 — Security:**
7. E3 — DNS-resolving SSRF guard (reject if any resolved A/AAAA is private/reserved/link-local; normalize int/octal/hex; pin IP for request).
8. E1 — apply SSRF validation to the **connection URL** at connect/verify (gated by admin-controlled localhost/private allowlist).
9. E2 — make `allow_localhost_oauth` (and other privileged policy keys) admin-only.
10. E4 — restrict write-tool name-whitelist; E5/E6 — state/session binding + clear tokens on url change.

**Phase 3 — Robustness & discovery:**
11. F1 — short dedicated discovery timeout + concurrent per-connection connect; F2 — raise init timeout; F3 — fix connect-cancel teardown; F4 — bound disconnect; F5 — keep structuredContent; F6 — dedupe tool_ids.
12. D1 — prefer header PRM, validate PRM `resource` matches server; D2 — OIDC + path-insert fallback; D3 — preserve `client_info` across disconnect.

**Phase 4 — UX & governance:**
13. G1/G2/G3/G4 — popup or return-to-chat OAuth, error handling, needs-reauth surfacing; G6 — wire/remove stores.
14. G5 — admin endpoints to list/disable/revoke per-user MCP connections.
15. B6 — converge the two OAuth implementations onto the RFC9728-correct one.
