-- Repair conversation_token_usage rows whose last_cache_read_tokens (the pill's
-- "R" segment) is a STALE warm-cache value left over from an earlier request.
--
-- Root cause: update_conversation_token_usage used to advance last_cache_read
-- only on a per-dimension `cache_read_tokens > 0` guard. A later cold-cache
-- request (provider reports prompt_tokens>0 but cached_tokens=0) therefore never
-- cleared the prior hit, so last_input_tokens reflected the new request while
-- last_cache_read_tokens stayed pinned to an OLDER request's cache read — e.g.
-- the pill showed "R 243.8k" after a request that actually read 0 from cache.
--
-- The code fix snapshots all three last_* values atomically off a single
-- own-turn-prompt gate, so new writes stay consistent. This realigns the already
-- corrupted rows: last_cache_read_tokens is set to the cache_read of the latest
-- OWN-turn ('chat', source_chat_id = attributed_chat_id) prompt-bearing event
-- whose prompt matches the stored last_input_tokens (the exact request the pill
-- already shows as "Latest Input"), so input and cache become consistent.
--
-- Tightly scoped + idempotent:
--   * Only touches a chat that HAS an own-turn event matching its stored
--     last_input_tokens (so phantom/clobbered rows handled by the other repair
--     scripts are left alone, and last_input itself is never modified here).
--   * The `IS DISTINCT FROM` guard makes this a no-op on already-correct rows.
-- Re-running changes nothing.
BEGIN;

WITH correct_cache AS (
    SELECT DISTINCT ON (e.attributed_chat_id)
        e.attributed_chat_id,
        e.cache_read_tokens
    FROM token_usage_event e
    JOIN conversation_token_usage c2 ON c2.chat_id = e.attributed_chat_id
    WHERE e.source_chat_id = e.attributed_chat_id
      AND e.source_type = 'chat'
      AND e.prompt_tokens > 0
      AND e.prompt_tokens = c2.last_input_tokens
    ORDER BY e.attributed_chat_id, e.created_at DESC, e.id DESC
)
UPDATE conversation_token_usage c
SET last_cache_read_tokens = cc.cache_read_tokens
FROM correct_cache cc
WHERE c.chat_id = cc.attributed_chat_id
  AND c.last_cache_read_tokens IS DISTINCT FROM cc.cache_read_tokens;

COMMIT;
