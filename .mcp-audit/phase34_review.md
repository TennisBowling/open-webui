# Phase 3+4 MCP Review

**Verdict:** 4 confirmed issues, worst severity **high**. No uncertain items.

## Confirmed

### 1. [HIGH] `disconnect()` raises `RuntimeError` on every remote MCP disconnect (LIFO cancel-scope violation)
- **File:** `backend/open_webui/utils/mcp/client.py:306-307` (disconnect); transports/session entered at `167` and `179-181` (connect)
- **Issue:** Phase 3 wrapped `await stack.aclose()` in `with anyio.move_on_after(MCP_DISCONNECT_TIMEOUT)`. The exit stack holds the streamablehttp/sse transport and the `ClientSession`, each of which opens an anyio task-group cancel scope during `connect()`, *outside* any `move_on_after` scope. `disconnect()` then exits those scopes *inside* a freshly-entered `move_on_after` scope, violating anyio's strict-LIFO check and raising `RuntimeError: Attempted to exit a cancel scope that isn't the current task's current cancel scope`. Fires on the happy path (deadline never triggers) for every `remote_http`/`remote_sse` disconnect — structural, not timeout-dependent. Reproduced on the repo's anyio 4.10.0 + MCP SDK 1.14.1.
- **Notes (severity corrected critical→high):** The transport is *not* fully leaked — SDK cleanup bodies (DELETE, httpx/stream close) complete before the RuntimeError fires. Production callers wrap `disconnect()` in `try/except Exception`, so the error is logged, not a hard crash. The genuinely harmful path is `connect()`'s failure handler (`client.py:186-190`): the `disconnect()` RuntimeError masks the original connect/init error (the `raise` on line 190 never runs), so failed connects surface a misleading cancel-scope error.
- **Fix:** Do not introduce a new anyio cancel scope around `stack.aclose()`. Either (a) revert to a bare `await stack.aclose()`, or (b) bound teardown with a mechanism that does not create an anyio cancel scope wrapping the aclose (e.g. `asyncio.wait_for`). Add a regression test that closes a stack containing a *real* anyio cancel scope (the existing `FakeStack` in `test_subagent_reliability.py:402` has no inner scope and misses this).

### 2. [MEDIUM] Malformed double-`?` OAuth callback URL when originating page already has a query string
- **File:** `src/lib/components/chat/MessageInput/IntegrationsMenu.svelte:101`; `src/lib/components/chat/Settings/Tools/PersonalMCPConnections.svelte:156`; `backend/open_webui/routers/mcp.py:398-434`
- **Issue:** Both OAuth-start callers build `returnTo = ${location.pathname}${location.search}`, so from `/?temporary-chat=true` the value is `/?temporary-chat=true`. `_safe_return_path` (`mcp.py:76-88`) rejects only non-`/`-prefix, `//`, `://`, `\` — no `?` check — so it passes and is stored verbatim. The callback then concatenates a raw `?` (`mcp.py:401/432/434`), producing `/?temporary-chat=true?mcp_oauth=connected` (two `?`). The reader at `src/routes/(app)/+layout.svelte:313-315` (`searchParams.get('mcp_oauth')`) finds no key, so the success/error toast never fires, and the pre-existing `temporary-chat` param is corrupted to `true?mcp_oauth=connected`. Token exchange still succeeds; only feedback/UX regresses. Such URLs are reachable (`Navbar.svelte:150` does `replaceState(null,'','?temporary-chat=true')`).
- **Fix:** Drop `location.search` from `returnTo` in both callers (use `location.pathname` only), or make the backend append the result param query-aware (`'&' if '?' in return_path else '?'`) and reject embedded `?` in `_safe_return_path`. Prefer a single query-aware append on the backend.

### 3. [LOW] `connect()` overwrites `self.exit_stack`, abandoning the stack entered by `__aenter__`
- **File:** `backend/open_webui/utils/mcp/client.py:145-147` (connect), `313-318` (`__aenter__`)
- **Issue:** `__aenter__` lazily allocates and enters `self.exit_stack`; `connect()` then unconditionally allocates a new `AsyncExitStack`, enters it, and reassigns `self.exit_stack`, discarding the `__aenter__` stack. In the documented `async with MCPClient() as c: await c.connect(...)` form, the `__aenter__` stack is entered but never closed. Regression from prior `connect()`, which used `async with AsyncExitStack()` + `pop_all()` and only set `self.exit_stack` on success.
- **Notes:** Latent — a repo-wide grep found no `async with MCPClient()` call site. The orphaned `__aenter__` stack is also *empty* (nothing registered before the overwrite), so it leaks no material resource; practical cost is ~nil.
- **Fix:** In `connect()`, reuse an already-entered stack instead of allocating a new one (e.g. `exit_stack = self.exit_stack or AsyncExitStack()`, only `__aenter__()` when freshly allocated), or have `__aenter__` not pre-allocate.

### 4. [LOW] D2: appended-after-path AS-metadata form dropped for path-bearing issuers (Notion unaffected)
- **File:** `backend/open_webui/utils/mcp/oauth.py:276-296, 354-366`
- **Issue:** Phase 3 replaced the single AS-metadata URL with multi-candidate `authorization_server_metadata_urls()`. For a path-bearing issuer (e.g. `https://auth.example.com/tenant1`), the old code probed the appended-after-path form `.../tenant1/.well-known/oauth-authorization-server`; the new code (lines 287-290) probes the RFC 8414 path-inserted form `.../.well-known/oauth-authorization-server/tenant1` and the root form, but never the old appended-after-path URL. A non-standard AS serving metadata only at the old location now fails discovery. Verified by direct execution: old URL is not in the new set for the path-bearing issuer.
- **Notes:** Notion is unaffected — its issuer is pathless (`https://mcp.notion.com`), for which the new first candidate is byte-identical to the old single URL and tried first. Standard RFC 8414 servers and all pathless issuers are unaffected.
- **Fix:** If backward compat is desired, also append the path-prefixed form `urljoin(base, f"{path}/.well-known/{wk}")` as a trailing fallback candidate. No change needed for Notion.

## Uncertain

None.

---

**Summary:** Four real, code-verified defects, no false positives. The only material one is the `disconnect()` cancel-scope RuntimeError (high): it fires on every remote MCP disconnect but is mostly absorbed by `try/except` at call sites — its real bite is masking the original error in `connect()`'s failure handler. The frontend double-`?` bug (medium) silently breaks OAuth success/error toasts and corrupts query params, but only when OAuth starts from a URL already bearing a query string (e.g. temporary-chat). The remaining two (low) are a latent empty-stack orphan in the unused context-manager API and a discovery regression that only touches non-RFC-8414 path-bearing authorization servers — neither affects Notion. Recommend fixing #1 first (remove the anyio cancel scope around `aclose()` and add a real-cancel-scope test), then #2; #3 and #4 are optional hardening.
