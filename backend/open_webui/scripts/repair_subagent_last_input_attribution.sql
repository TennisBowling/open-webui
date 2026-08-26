-- Corrective repair: a subagent run's usage event had been written as the
-- visible parent chat's "Latest Input"/"Latest Output" snapshot (the pill shows
-- conversation_token_usage.last_input_tokens). The code fix (is_own_turn gating
-- in update_conversation_token_usage) stops this going forward; this restores the
-- snapshot to each affected chat's latest OWN-turn ('chat', source_chat_id =
-- attributed_chat_id) non-zero event. Subagent tokens remain in the totals.
--
-- Idempotent and tightly scoped: only updates a conversation whose chronologically
-- LATEST non-zero event is a subagent event AND whose stored last_input currently
-- equals that subagent event's prompt (the exact bug signature). Re-running is a
-- no-op.
BEGIN;

WITH latest_any AS (
    SELECT DISTINCT ON (attributed_chat_id)
        attributed_chat_id, source_chat_id, prompt_tokens
    FROM token_usage_event
    WHERE (prompt_tokens > 0 OR completion_tokens > 0 OR total_tokens > 0)
      AND source_type IN ('chat', 'subagent')
    ORDER BY attributed_chat_id, created_at DESC, id DESC
),
own_latest AS (
    SELECT DISTINCT ON (attributed_chat_id)
        attributed_chat_id, prompt_tokens, completion_tokens, cache_read_tokens
    FROM token_usage_event
    WHERE source_chat_id = attributed_chat_id
      AND source_type = 'chat'
      AND (prompt_tokens > 0 OR completion_tokens > 0 OR total_tokens > 0)
    ORDER BY attributed_chat_id, created_at DESC, id DESC
)
UPDATE conversation_token_usage c
SET last_input_tokens = o.prompt_tokens,
    last_output_tokens = o.completion_tokens,
    last_cache_read_tokens = o.cache_read_tokens
FROM latest_any la
JOIN own_latest o ON o.attributed_chat_id = la.attributed_chat_id
WHERE c.chat_id = la.attributed_chat_id
  AND la.source_chat_id <> la.attributed_chat_id   -- latest real event was a subagent
  AND c.last_input_tokens = la.prompt_tokens;       -- pill currently shows that subagent value

COMMIT;
