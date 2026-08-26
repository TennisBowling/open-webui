-- Corrective repair (phantom snapshot): some conversations had their
-- last_input/last_output/last_cache_read snapshot clobbered by a NON-ZERO but
-- WRONG value from a pre-fix hidden-run write (a legacy_subagent_aggregate or
-- subagent event that landed after the parent's own last turn). The pill renders
-- last_input_tokens ("Latest Input") and last_cache_read_tokens (the "R"
-- segment), so these show a stale figure that belongs to a different run/chat.
--
-- The original zero-only repair (repair_clobbered_last_input_tokens.sql) used
-- `WHERE last_input_tokens = 0` and could not see these non-zero phantoms. The
-- is_own_turn gating in update_conversation_token_usage stops new occurrences;
-- this restores the snapshot from each chat's latest OWN-turn ('chat',
-- source_chat_id = attributed_chat_id) non-zero event.
--
-- Tightly scoped + idempotent: only touches a conversation whose stored
-- last_input_tokens matches NO own-turn event of that chat (the phantom
-- signature). This is a no-op on already-correct rows and on same-second
-- ordering ties (whose stored value DOES match an own-turn event). Re-running
-- changes nothing.
BEGIN;

WITH own_latest AS (
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
FROM own_latest o
WHERE c.chat_id = o.attributed_chat_id
  AND NOT EXISTS (
        SELECT 1
        FROM token_usage_event e
        WHERE e.attributed_chat_id = c.chat_id
          AND e.source_chat_id = e.attributed_chat_id
          AND e.source_type = 'chat'
          AND e.prompt_tokens = c.last_input_tokens
  );

COMMIT;
