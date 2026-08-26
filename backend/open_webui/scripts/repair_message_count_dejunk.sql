-- Recompute message_count on the three analytics aggregate tables to the number
-- of REAL model requests (SUM(request_count) over non-zero events), removing the
-- inflation from the provider's all-zero streaming-usage chunks.
--
-- Why message_count and not tokens/cost: the all-zero junk events carry 0 tokens
-- and $0 cost, so they never inflated any token/cost SUM — only the per-event
-- counters (message_count was +1 per process_token_usage call, including each junk
-- chunk). The ingestion guard (usage_has_data) stops new junk; this corrects the
-- historical counters from the source-of-truth event log (which covers the full
-- date range). No rows are deleted; token/cost columns are untouched.
--
-- Idempotent: message_count is set to the freshly-computed non-zero request count
-- each run, so re-running changes nothing. NOTE: model_token_usage also carries a
-- pre-existing (unrelated) drift in its token totals — NOT corrected here.
BEGIN;

-- non-zero predicate, inline (e.* alias inside the correlated subqueries)
-- (prompt_tokens<>0 OR completion_tokens<>0 OR total_tokens<>0 OR COALESCE(cache_read_tokens,0)<>0)

-- 1) conversation_token_usage: per attributed_chat_id
UPDATE conversation_token_usage c
SET message_count = COALESCE((
        SELECT SUM(e.request_count)
        FROM token_usage_event e
        WHERE e.attributed_chat_id = c.chat_id
          AND (e.prompt_tokens <> 0 OR e.completion_tokens <> 0
               OR e.total_tokens <> 0 OR COALESCE(e.cache_read_tokens, 0) <> 0)
    ), 0);

-- 2) daily_token_usage: per (user_id, UTC date)
UPDATE daily_token_usage d
SET message_count = COALESCE((
        SELECT SUM(e.request_count)
        FROM token_usage_event e
        WHERE e.user_id = d.user_id
          AND to_char(to_timestamp(e.created_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD') = d.date
          AND (e.prompt_tokens <> 0 OR e.completion_tokens <> 0
               OR e.total_tokens <> 0 OR COALESCE(e.cache_read_tokens, 0) <> 0)
    ), 0);

-- 3a) model_token_usage per-user rows: per (user_id, model_id)
UPDATE model_token_usage m
SET message_count = COALESCE((
        SELECT SUM(e.request_count)
        FROM token_usage_event e
        WHERE e.user_id = m.user_id
          AND e.model_id = m.model_id
          AND (e.prompt_tokens <> 0 OR e.completion_tokens <> 0
               OR e.total_tokens <> 0 OR COALESCE(e.cache_read_tokens, 0) <> 0)
    ), 0)
WHERE m.user_id IS NOT NULL;

-- 3b) model_token_usage global rows (user_id IS NULL): per model_id across all users
UPDATE model_token_usage m
SET message_count = COALESCE((
        SELECT SUM(e.request_count)
        FROM token_usage_event e
        WHERE e.model_id = m.model_id
          AND (e.prompt_tokens <> 0 OR e.completion_tokens <> 0
               OR e.total_tokens <> 0 OR COALESCE(e.cache_read_tokens, 0) <> 0)
    ), 0)
WHERE m.user_id IS NULL;

COMMIT;
