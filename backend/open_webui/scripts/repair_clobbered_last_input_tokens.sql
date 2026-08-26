-- Repair conversation_token_usage rows whose last_input_tokens were clobbered
-- to 0 by zero-filled intermediate usage chunks (the bare-id "C" gemini
-- provider emits all-zero `usage` objects on most streaming chunks; the old
-- ingestion path recorded each one and overwrote last_input/last_output to 0,
-- so the in-chat pill's "Latest Input" segment read 0 after agentic turns).
--
-- The code fix (usage_has_data guard in process_token_usage + middleware) stops
-- new corruption. This restores last_input/last_output/last_cache_read for the
-- already-corrupted conversations from the most recent NON-ZERO event of each
-- chat. Idempotent: only touches rows currently sitting at last_input_tokens = 0
-- that have a real event to restore from.
BEGIN;

WITH latest_real AS (
    SELECT DISTINCT ON (attributed_chat_id)
        attributed_chat_id,
        prompt_tokens,
        completion_tokens,
        cache_read_tokens
    FROM token_usage_event
    WHERE (prompt_tokens > 0 OR completion_tokens > 0 OR total_tokens > 0)
      -- Snapshot the visible chat's OWN turns only: exclude hidden subagent runs
      -- (source_chat_id <> attributed_chat_id) and offline legacy rollups, so the
      -- pill's "Latest Input" never shows an internal subagent request's size.
      AND source_chat_id = attributed_chat_id
      AND source_type = 'chat'
    ORDER BY attributed_chat_id, created_at DESC, id DESC
)
UPDATE conversation_token_usage c
SET last_input_tokens = lr.prompt_tokens,
    last_output_tokens = lr.completion_tokens,
    last_cache_read_tokens = lr.cache_read_tokens
FROM latest_real lr
WHERE c.chat_id = lr.attributed_chat_id
  AND c.last_input_tokens = 0;

COMMIT;
