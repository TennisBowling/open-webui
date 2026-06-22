#!/usr/bin/env python3
"""Rebuild token analytics tables on PostgreSQL.

Sources:
- chat_message.meta->usage for raw per-request usage
- conversation_token_usage legacy aggregate rows without raw events, unless
  --raw-only is supplied

This script is Postgres-only and async. It intentionally does not support the
old SQLite runtime.
"""

import argparse
import asyncio
import logging
import sys

from sqlalchemy import text

from open_webui.internal.db import get_db


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


def _usage_expr(path: str, default: str = "0") -> str:
    return f"COALESCE(NULLIF(cm.meta #>> '{{usage,{path}}}', '')::bigint, {default})"


async def _prepare_temp_tables(db, include_legacy_aggregates: bool) -> None:
    await db.execute(text("DROP TABLE IF EXISTS temp_raw_usage_events"))
    await db.execute(
        text(
            f"""
            CREATE TEMP TABLE temp_raw_usage_events AS
            SELECT
                COALESCE(NULLIF(c.subagent_of, ''), cm.chat_id) AS chat_id,
                cm.chat_id AS source_chat_id,
                cm.message_id AS message_id,
                COALESCE(p.user_id, c.user_id) AS user_id,
                COALESCE(
                    NULLIF(cm.meta->>'selectedModelId', ''),
                    NULLIF(cm.model, ''),
                    NULLIF(c.model_id_primary, ''),
                    'unknown'
                ) AS model_id,
                {_usage_expr('prompt_tokens')} AS prompt_tokens,
                {_usage_expr('completion_tokens')} AS completion_tokens,
                COALESCE(
                    NULLIF(cm.meta #>> '{{usage,total_tokens}}', '')::bigint,
                    {_usage_expr('prompt_tokens')} + {_usage_expr('completion_tokens')}
                ) AS request_total_tokens,
                {_usage_expr('prompt_tokens_details,cached_tokens')} AS cache_read_tokens,
                {_usage_expr('prompt_tokens')} AS latest_input_tokens,
                {_usage_expr('completion_tokens')} AS latest_output_tokens,
                {_usage_expr('prompt_tokens_details,cached_tokens')} AS latest_cache_read_tokens,
                COALESCE(cm.timestamp, c.updated_at, c.created_at, 0) AS event_ts,
                cm.sequence AS sequence,
                1::bigint AS request_count,
                'raw' AS source_type
            FROM chat_message cm
            JOIN chat c ON c.id = cm.chat_id
            LEFT JOIN chat p ON p.id = COALESCE(NULLIF(c.subagent_of, ''), cm.chat_id)
            WHERE COALESCE(p.user_id, c.user_id) NOT LIKE 'shared-%'
              AND cm.meta IS NOT NULL
              AND jsonb_typeof(cm.meta->'usage') = 'object'
            """
        )
    )

    await db.execute(text("DROP TABLE IF EXISTS temp_legacy_usage_events"))
    if include_legacy_aggregates:
        await db.execute(
            text(
                """
                CREATE TEMP TABLE temp_legacy_usage_events AS
                WITH raw_source_chats AS (
                    SELECT DISTINCT source_chat_id FROM temp_raw_usage_events
                )
                SELECT
                    COALESCE(NULLIF(ch.subagent_of, ''), ctu.chat_id) AS chat_id,
                    ctu.chat_id AS source_chat_id,
                    ctu.chat_id AS message_id,
                    COALESCE(parent.user_id, ctu.user_id) AS user_id,
                    COALESCE(NULLIF(ctu.model_id, ''), NULLIF(ch.model_id_primary, ''), 'unknown') AS model_id,
                    CASE
                        WHEN COALESCE(ctu.total_tokens, 0) >= COALESCE(ctu.total_output_tokens, 0)
                            THEN COALESCE(ctu.total_tokens, 0) - COALESCE(ctu.total_output_tokens, 0)
                        ELSE COALESCE(ctu.total_input_tokens, 0)
                    END AS prompt_tokens,
                    COALESCE(ctu.total_output_tokens, 0) AS completion_tokens,
                    COALESCE(ctu.total_tokens, 0) AS request_total_tokens,
                    COALESCE(ctu.total_cache_read_tokens, 0) AS cache_read_tokens,
                    COALESCE(ctu.last_input_tokens, ctu.total_input_tokens, 0) AS latest_input_tokens,
                    COALESCE(ctu.last_output_tokens, 0) AS latest_output_tokens,
                    COALESCE(ctu.last_cache_read_tokens, 0) AS latest_cache_read_tokens,
                    COALESCE(ctu.updated_at, ctu.created_at, ch.updated_at, ch.created_at, 0) AS event_ts,
                    0 AS sequence,
                    COALESCE(ctu.message_count, 1) AS request_count,
                    CASE
                        WHEN ch.subagent_of IS NOT NULL AND ch.subagent_of <> ''
                            THEN 'legacy_subagent_aggregate'
                        ELSE 'legacy_aggregate'
                    END AS source_type
                FROM conversation_token_usage ctu
                LEFT JOIN raw_source_chats raw ON raw.source_chat_id = ctu.chat_id
                LEFT JOIN chat ch ON ch.id = ctu.chat_id
                LEFT JOIN chat parent ON parent.id = ch.subagent_of
                WHERE COALESCE(parent.user_id, ctu.user_id) NOT LIKE 'shared-%'
                  AND raw.source_chat_id IS NULL
                """
            )
        )
    else:
        await db.execute(
            text("CREATE TEMP TABLE temp_legacy_usage_events AS SELECT * FROM temp_raw_usage_events WHERE false")
        )

    await db.execute(text("DROP TABLE IF EXISTS temp_usage_events"))
    await db.execute(
        text(
            """
            CREATE TEMP TABLE temp_usage_events AS
            SELECT * FROM temp_raw_usage_events
            UNION ALL
            SELECT * FROM temp_legacy_usage_events
            """
        )
    )
    await db.execute(text("CREATE INDEX temp_usage_events_chat_idx ON temp_usage_events(chat_id)"))
    await db.execute(text("CREATE INDEX temp_usage_events_daily_idx ON temp_usage_events(user_id, event_ts)"))
    await db.execute(text("CREATE INDEX temp_usage_events_model_idx ON temp_usage_events(user_id, model_id)"))

    await db.execute(text("DROP TABLE IF EXISTS temp_conv_latest"))
    await db.execute(
        text(
            """
            CREATE TEMP TABLE temp_conv_latest AS
            SELECT chat_id, model_id, latest_input_tokens, latest_output_tokens, latest_cache_read_tokens
            FROM (
                SELECT e.*, ROW_NUMBER() OVER (
                    PARTITION BY chat_id ORDER BY event_ts DESC, sequence DESC, message_id DESC
                ) AS rn
                FROM temp_usage_events e
            ) ranked
            WHERE rn = 1
            """
        )
    )

    await db.execute(text("DROP TABLE IF EXISTS temp_conv_agg"))
    await db.execute(
        text(
            """
            CREATE TEMP TABLE temp_conv_agg AS
            SELECT
                e.chat_id,
                MAX(e.user_id) AS user_id,
                l.model_id,
                SUM(e.prompt_tokens) AS total_input_tokens,
                SUM(e.completion_tokens) AS total_output_tokens,
                SUM(e.request_total_tokens) AS total_tokens,
                SUM(e.cache_read_tokens) AS total_cache_read_tokens,
                l.latest_input_tokens AS last_input_tokens,
                l.latest_output_tokens AS last_output_tokens,
                l.latest_cache_read_tokens AS last_cache_read_tokens,
                SUM(e.request_count) AS message_count,
                MIN(e.event_ts) AS created_at,
                MAX(e.event_ts) AS updated_at
            FROM temp_usage_events e
            JOIN temp_conv_latest l ON l.chat_id = e.chat_id
            GROUP BY e.chat_id, l.model_id, l.latest_input_tokens, l.latest_output_tokens, l.latest_cache_read_tokens
            """
        )
    )

    await db.execute(text("DROP TABLE IF EXISTS temp_daily_agg"))
    await db.execute(
        text(
            """
            CREATE TEMP TABLE temp_daily_agg AS
            SELECT
                user_id,
                to_char(to_timestamp(event_ts) AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS date,
                SUM(prompt_tokens) AS total_input_tokens,
                SUM(completion_tokens) AS total_output_tokens,
                SUM(request_total_tokens) AS total_tokens,
                SUM(cache_read_tokens) AS total_cache_read_tokens,
                COUNT(DISTINCT chat_id) AS conversation_count,
                SUM(request_count) AS message_count,
                MIN(event_ts) AS created_at,
                MAX(event_ts) AS updated_at
            FROM temp_usage_events
            GROUP BY user_id, to_char(to_timestamp(event_ts) AT TIME ZONE 'UTC', 'YYYY-MM-DD')
            """
        )
    )

    await db.execute(text("DROP TABLE IF EXISTS temp_model_user_agg"))
    await db.execute(
        text(
            """
            CREATE TEMP TABLE temp_model_user_agg AS
            SELECT
                user_id, model_id,
                SUM(prompt_tokens) AS total_input_tokens,
                SUM(completion_tokens) AS total_output_tokens,
                SUM(request_total_tokens) AS total_tokens,
                SUM(cache_read_tokens) AS total_cache_read_tokens,
                COUNT(DISTINCT chat_id) AS conversation_count,
                SUM(request_count) AS message_count,
                MIN(event_ts) AS created_at,
                MAX(event_ts) AS updated_at
            FROM temp_usage_events
            GROUP BY user_id, model_id
            """
        )
    )

    await db.execute(text("DROP TABLE IF EXISTS temp_model_global_agg"))
    await db.execute(
        text(
            """
            CREATE TEMP TABLE temp_model_global_agg AS
            SELECT
                model_id,
                SUM(prompt_tokens) AS total_input_tokens,
                SUM(completion_tokens) AS total_output_tokens,
                SUM(request_total_tokens) AS total_tokens,
                SUM(cache_read_tokens) AS total_cache_read_tokens,
                COUNT(DISTINCT chat_id) AS conversation_count,
                SUM(request_count) AS message_count,
                MIN(event_ts) AS created_at,
                MAX(event_ts) AS updated_at
            FROM temp_usage_events
            GROUP BY model_id
            """
        )
    )


async def _print_summary(db, label: str) -> None:
    row = (
        await db.execute(
            text(
                """
                SELECT
                    COUNT(*) AS event_or_legacy_rows,
                    SUM(request_count) AS request_count,
                    SUM(CASE WHEN source_type = 'raw' THEN 1 ELSE 0 END) AS raw_events,
                    SUM(CASE WHEN source_type = 'legacy_subagent_aggregate' THEN 1 ELSE 0 END) AS legacy_subagent_rows,
                    SUM(CASE WHEN source_type = 'legacy_aggregate' THEN 1 ELSE 0 END) AS legacy_rows,
                    COUNT(DISTINCT chat_id) AS chats,
                    COUNT(DISTINCT user_id) AS users,
                    COALESCE(SUM(prompt_tokens), 0) AS prompt_sum,
                    COALESCE(SUM(completion_tokens), 0) AS output_sum,
                    COALESCE(SUM(request_total_tokens), 0) AS total_sum,
                    COALESCE(SUM(cache_read_tokens), 0) AS cache_sum
                FROM temp_usage_events
                """
            )
        )
    ).mappings().first()
    log.info("%s source summary: %s", label, dict(row or {}))


async def _rebuild(dry_run: bool, include_legacy_aggregates: bool) -> None:
    async with get_db() as db:
        await _prepare_temp_tables(db, include_legacy_aggregates=include_legacy_aggregates)
        await _print_summary(db, "computed")

        if dry_run:
            await db.rollback()
            log.info("Dry run complete; no analytics tables modified.")
            return

        for table_name in (
            "token_usage_event",
            "conversation_token_usage",
            "daily_token_usage",
            "model_token_usage",
        ):
            await db.execute(text(f"DELETE FROM {table_name}"))

        await db.execute(
            text(
                """
                INSERT INTO token_usage_event (
                    id, user_id, source_chat_id, attributed_chat_id,
                    message_id, parent_message_id, model_id,
                    prompt_tokens, completion_tokens, total_tokens, cache_read_tokens,
                    request_count, source_type, raw_usage, created_at
                )
                SELECT
                    md5(random()::text || clock_timestamp()::text || source_chat_id || message_id),
                    user_id, source_chat_id, chat_id,
                    message_id, NULL, model_id,
                    prompt_tokens, completion_tokens, request_total_tokens, cache_read_tokens,
                    request_count, source_type,
                    CASE
                        WHEN source_type = 'raw' THEN jsonb_build_object(
                            'prompt_tokens', prompt_tokens,
                            'completion_tokens', completion_tokens,
                            'total_tokens', request_total_tokens,
                            'prompt_tokens_details', jsonb_build_object('cached_tokens', cache_read_tokens)
                        )
                        ELSE jsonb_build_object('legacy_aggregate', true, 'request_count', request_count)
                    END,
                    event_ts
                FROM temp_usage_events
                """
            )
        )

        await db.execute(
            text(
                """
                INSERT INTO conversation_token_usage (
                    id, chat_id, user_id, model_id,
                    total_input_tokens, total_output_tokens, total_tokens, total_cache_read_tokens,
                    last_input_tokens, last_output_tokens, last_cache_read_tokens,
                    message_count, created_at, updated_at
                )
                SELECT
                    md5(random()::text || clock_timestamp()::text || chat_id), chat_id, user_id, model_id,
                    total_input_tokens, total_output_tokens, total_tokens, total_cache_read_tokens,
                    last_input_tokens, last_output_tokens, last_cache_read_tokens,
                    message_count, created_at, updated_at
                FROM temp_conv_agg
                """
            )
        )

        await db.execute(
            text(
                """
                INSERT INTO daily_token_usage (
                    id, user_id, date,
                    total_input_tokens, total_output_tokens, total_tokens, total_cache_read_tokens,
                    conversation_count, message_count, created_at, updated_at
                )
                SELECT
                    md5(random()::text || clock_timestamp()::text || user_id || date), user_id, date,
                    total_input_tokens, total_output_tokens, total_tokens, total_cache_read_tokens,
                    conversation_count, message_count, created_at, updated_at
                FROM temp_daily_agg
                """
            )
        )

        await db.execute(
            text(
                """
                INSERT INTO model_token_usage (
                    id, user_id, model_id,
                    total_input_tokens, total_output_tokens, total_tokens, total_cache_read_tokens,
                    conversation_count, message_count, created_at, updated_at
                )
                SELECT
                    md5(random()::text || clock_timestamp()::text || user_id || model_id), user_id, model_id,
                    total_input_tokens, total_output_tokens, total_tokens, total_cache_read_tokens,
                    conversation_count, message_count, created_at, updated_at
                FROM temp_model_user_agg
                UNION ALL
                SELECT
                    md5(random()::text || clock_timestamp()::text || model_id), NULL, model_id,
                    total_input_tokens, total_output_tokens, total_tokens, total_cache_read_tokens,
                    conversation_count, message_count, created_at, updated_at
                FROM temp_model_global_agg
                """
            )
        )

        await db.commit()
        await _print_summary(db, "written")
        log.info("Analytics rebuild complete.")


async def amain() -> None:
    parser = argparse.ArgumentParser(description="Rebuild Open WebUI token analytics on PostgreSQL")
    parser.add_argument("--dry-run", action="store_true", help="Compute totals without modifying analytics tables")
    parser.add_argument("--raw-only", action="store_true", help="Ignore legacy aggregate-only conversation rows")
    args = parser.parse_args()
    await _rebuild(args.dry_run, include_legacy_aggregates=not args.raw_only)


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
