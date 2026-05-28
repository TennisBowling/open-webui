#!/usr/bin/env python3
"""Rebuild token analytics tables from persisted usage.

Primary source of truth:
    chat_message.meta.usage

Compatibility source:
    conversation_token_usage rows that have no backing chat_message.meta.usage.
    This preserves older aggregate-only counters, especially legacy hidden
    subagent chats whose per-request usage was never persisted. Hidden subagent
    aggregate rows are attributed to their visible parent chat and do NOT count
    as standalone conversations.

Semantics after rebuild:
    total_input_tokens        = SUM(usage.prompt_tokens) for raw events;
                                for legacy aggregates, total_tokens-output_tokens
                                so input+output remains consistent.
    total_output_tokens       = SUM(usage.completion_tokens)
    total_tokens              = SUM(usage.total_tokens), fallback prompt+completion
    total_cache_read_tokens   = SUM(usage.prompt_tokens_details.cached_tokens)
    last_input_tokens         = latest request usage.prompt_tokens per chat
    last_output_tokens        = latest request usage.completion_tokens per chat
    last_cache_read_tokens    = latest request cached_tokens per chat
    message_count             = request/model-call count, not visible chat messages

Shared chat copies (user_id LIKE 'shared-%') are excluded to avoid double-counting.
"""

import argparse
import logging
import sys

from sqlalchemy import inspect, text

from open_webui.internal.db import get_db


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


CACHE_COLUMNS = {
    "conversation_token_usage": [
        "total_cache_read_tokens",
        "last_cache_read_tokens",
    ],
    "daily_token_usage": ["total_cache_read_tokens"],
    "model_token_usage": ["total_cache_read_tokens"],
}


def _ensure_columns(db) -> None:
    inspector = inspect(db.bind)
    for table, columns in CACHE_COLUMNS.items():
        existing = {col["name"] for col in inspector.get_columns(table)}
        for col in columns:
            if col not in existing:
                log.info("Adding missing column %s.%s", table, col)
                db.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} BIGINT DEFAULT 0"))

    tables = set(inspector.get_table_names())
    if "token_usage_event" not in tables:
        log.info("Creating token_usage_event table")
        db.execute(text("""
            CREATE TABLE token_usage_event (
                id VARCHAR PRIMARY KEY,
                user_id VARCHAR,
                source_chat_id VARCHAR,
                attributed_chat_id VARCHAR,
                message_id VARCHAR,
                parent_message_id VARCHAR,
                model_id VARCHAR,
                prompt_tokens BIGINT DEFAULT 0,
                completion_tokens BIGINT DEFAULT 0,
                total_tokens BIGINT DEFAULT 0,
                cache_read_tokens BIGINT DEFAULT 0,
                request_count INTEGER DEFAULT 1,
                source_type VARCHAR DEFAULT 'chat',
                raw_usage JSON,
                created_at BIGINT
            )
        """))
        db.execute(text("CREATE INDEX token_usage_event_attr_chat_idx ON token_usage_event(attributed_chat_id)"))
        db.execute(text("CREATE INDEX token_usage_event_source_chat_idx ON token_usage_event(source_chat_id)"))
        db.execute(text("CREATE INDEX token_usage_event_user_ts_idx ON token_usage_event(user_id, created_at)"))
        db.execute(text("CREATE INDEX token_usage_event_model_idx ON token_usage_event(model_id)"))


def _prepare_temp_tables(db, include_legacy_aggregates: bool) -> None:
    db.execute(text("DROP TABLE IF EXISTS temp_raw_usage_events"))
    db.execute(
        text(
            """
            CREATE TEMP TABLE temp_raw_usage_events AS
            SELECT
                COALESCE(NULLIF(c.subagent_of, ''), cm.chat_id) AS chat_id,
                cm.chat_id AS source_chat_id,
                cm.message_id AS message_id,
                COALESCE(p.user_id, c.user_id) AS user_id,
                COALESCE(
                    NULLIF(json_extract(cm.meta, '$.selectedModelId'), ''),
                    NULLIF(cm.model, ''),
                    NULLIF(c.model_id_primary, ''),
                    'unknown'
                ) AS model_id,
                CAST(COALESCE(json_extract(cm.meta, '$.usage.prompt_tokens'), 0) AS INTEGER) AS prompt_tokens,
                CAST(COALESCE(json_extract(cm.meta, '$.usage.completion_tokens'), 0) AS INTEGER) AS completion_tokens,
                CAST(COALESCE(
                    json_extract(cm.meta, '$.usage.total_tokens'),
                    COALESCE(json_extract(cm.meta, '$.usage.prompt_tokens'), 0)
                    + COALESCE(json_extract(cm.meta, '$.usage.completion_tokens'), 0)
                ) AS INTEGER) AS request_total_tokens,
                CAST(COALESCE(json_extract(cm.meta, '$.usage.prompt_tokens_details.cached_tokens'), 0) AS INTEGER) AS cache_read_tokens,
                CAST(COALESCE(json_extract(cm.meta, '$.usage.prompt_tokens'), 0) AS INTEGER) AS latest_input_tokens,
                CAST(COALESCE(json_extract(cm.meta, '$.usage.completion_tokens'), 0) AS INTEGER) AS latest_output_tokens,
                CAST(COALESCE(json_extract(cm.meta, '$.usage.prompt_tokens_details.cached_tokens'), 0) AS INTEGER) AS latest_cache_read_tokens,
                COALESCE(cm.timestamp, c.updated_at, c.created_at, 0) AS event_ts,
                cm.sequence AS sequence,
                1 AS request_count,
                'raw' AS source_type
            FROM chat_message cm
            JOIN chat c ON c.id = cm.chat_id
            LEFT JOIN chat p ON p.id = COALESCE(NULLIF(c.subagent_of, ''), cm.chat_id)
            WHERE COALESCE(p.user_id, c.user_id) NOT LIKE 'shared-%'
              AND json_valid(cm.meta) = 1
              AND json_type(cm.meta, '$.usage') IS NOT NULL
            """
        )
    )

    db.execute(text("DROP TABLE IF EXISTS temp_legacy_usage_events"))
    if include_legacy_aggregates:
        db.execute(
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
                    CAST(
                        CASE
                            WHEN COALESCE(ctu.total_tokens, 0) >= COALESCE(ctu.total_output_tokens, 0)
                                THEN COALESCE(ctu.total_tokens, 0) - COALESCE(ctu.total_output_tokens, 0)
                            ELSE COALESCE(ctu.total_input_tokens, 0)
                        END AS INTEGER
                    ) AS prompt_tokens,
                    CAST(COALESCE(ctu.total_output_tokens, 0) AS INTEGER) AS completion_tokens,
                    CAST(COALESCE(ctu.total_tokens, 0) AS INTEGER) AS request_total_tokens,
                    CAST(COALESCE(ctu.total_cache_read_tokens, 0) AS INTEGER) AS cache_read_tokens,
                    CAST(COALESCE(ctu.last_input_tokens, ctu.total_input_tokens, 0) AS INTEGER) AS latest_input_tokens,
                    CAST(COALESCE(ctu.last_output_tokens, 0) AS INTEGER) AS latest_output_tokens,
                    CAST(COALESCE(ctu.last_cache_read_tokens, 0) AS INTEGER) AS latest_cache_read_tokens,
                    COALESCE(ctu.updated_at, ctu.created_at, ch.updated_at, ch.created_at, 0) AS event_ts,
                    0 AS sequence,
                    CAST(COALESCE(ctu.message_count, 1) AS INTEGER) AS request_count,
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
        db.execute(
            text(
                """
                CREATE TEMP TABLE temp_legacy_usage_events AS
                SELECT * FROM temp_raw_usage_events WHERE 0
                """
            )
        )

    db.execute(text("DROP TABLE IF EXISTS temp_usage_events"))
    db.execute(
        text(
            """
            CREATE TEMP TABLE temp_usage_events AS
            SELECT * FROM temp_raw_usage_events
            UNION ALL
            SELECT * FROM temp_legacy_usage_events
            """
        )
    )
    db.execute(text("CREATE INDEX temp_usage_events_chat_idx ON temp_usage_events(chat_id)"))
    db.execute(text("CREATE INDEX temp_usage_events_daily_idx ON temp_usage_events(user_id, event_ts)"))
    db.execute(text("CREATE INDEX temp_usage_events_model_idx ON temp_usage_events(user_id, model_id)"))

    db.execute(text("DROP TABLE IF EXISTS temp_conv_latest"))
    db.execute(
        text(
            """
            CREATE TEMP TABLE temp_conv_latest AS
            SELECT chat_id, model_id, latest_input_tokens, latest_output_tokens, latest_cache_read_tokens
            FROM (
                SELECT
                    e.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY chat_id
                        ORDER BY event_ts DESC, sequence DESC, message_id DESC
                    ) AS rn
                FROM temp_usage_events e
            )
            WHERE rn = 1
            """
        )
    )

    db.execute(text("DROP TABLE IF EXISTS temp_conv_agg"))
    db.execute(
        text(
            """
            CREATE TEMP TABLE temp_conv_agg AS
            SELECT
                e.chat_id,
                MAX(e.user_id) AS user_id,
                l.model_id AS model_id,
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
            GROUP BY e.chat_id
            """
        )
    )

    db.execute(text("DROP TABLE IF EXISTS temp_daily_agg"))
    db.execute(
        text(
            """
            CREATE TEMP TABLE temp_daily_agg AS
            SELECT
                user_id,
                date(event_ts, 'unixepoch') AS date,
                SUM(prompt_tokens) AS total_input_tokens,
                SUM(completion_tokens) AS total_output_tokens,
                SUM(request_total_tokens) AS total_tokens,
                SUM(cache_read_tokens) AS total_cache_read_tokens,
                COUNT(DISTINCT chat_id) AS conversation_count,
                SUM(request_count) AS message_count,
                MIN(event_ts) AS created_at,
                MAX(event_ts) AS updated_at
            FROM temp_usage_events
            GROUP BY user_id, date(event_ts, 'unixepoch')
            """
        )
    )

    db.execute(text("DROP TABLE IF EXISTS temp_model_user_agg"))
    db.execute(
        text(
            """
            CREATE TEMP TABLE temp_model_user_agg AS
            SELECT
                user_id,
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
            GROUP BY user_id, model_id
            """
        )
    )

    db.execute(text("DROP TABLE IF EXISTS temp_model_global_agg"))
    db.execute(
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


def _print_summary(db, label: str) -> None:
    row = db.execute(
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
    ).mappings().first()
    log.info("%s source summary: %s", label, dict(row or {}))

    for name, table in [
        ("conversation", "temp_conv_agg"),
        ("daily", "temp_daily_agg"),
        ("model_user", "temp_model_user_agg"),
        ("model_global", "temp_model_global_agg"),
    ]:
        totals = db.execute(
            text(
                f"""
                SELECT
                    COUNT(*) AS rows,
                    COALESCE(SUM(total_input_tokens), 0) AS total_input,
                    COALESCE(SUM(total_output_tokens), 0) AS total_output,
                    COALESCE(SUM(total_tokens), 0) AS total,
                    COALESCE(SUM(total_cache_read_tokens), 0) AS cache_read,
                    COALESCE(SUM(message_count), 0) AS request_count
                FROM {table}
                """
            )
        ).mappings().first()
        log.info("%s %s: %s", label, name, dict(totals or {}))


def _rebuild(db, dry_run: bool, include_legacy_aggregates: bool) -> None:
    _ensure_columns(db)
    _prepare_temp_tables(db, include_legacy_aggregates=include_legacy_aggregates)
    _print_summary(db, "computed")

    if dry_run:
        db.rollback()
        log.info("Dry run complete; no analytics tables modified.")
        return

    db.execute(text("DELETE FROM token_usage_event"))
    db.execute(text("DELETE FROM conversation_token_usage"))
    db.execute(text("DELETE FROM daily_token_usage"))
    db.execute(text("DELETE FROM model_token_usage"))

    db.execute(
        text(
            """
            INSERT INTO token_usage_event (
                id, user_id, source_chat_id, attributed_chat_id,
                message_id, parent_message_id, model_id,
                prompt_tokens, completion_tokens, total_tokens, cache_read_tokens,
                request_count, source_type, raw_usage, created_at
            )
            SELECT
                lower(hex(randomblob(16))), user_id, source_chat_id, chat_id,
                message_id, NULL, model_id,
                prompt_tokens, completion_tokens, request_total_tokens, cache_read_tokens,
                request_count, source_type,
                CASE
                    WHEN source_type = 'raw' THEN json_object(
                        'prompt_tokens', prompt_tokens,
                        'completion_tokens', completion_tokens,
                        'total_tokens', request_total_tokens,
                        'prompt_tokens_details', json_object('cached_tokens', cache_read_tokens)
                    )
                    ELSE json_object('legacy_aggregate', 1, 'request_count', request_count)
                END,
                event_ts
            FROM temp_usage_events
            """
        )
    )

    db.execute(
        text(
            """
            INSERT INTO conversation_token_usage (
                id, chat_id, user_id, model_id,
                total_input_tokens, total_output_tokens, total_tokens, total_cache_read_tokens,
                last_input_tokens, last_output_tokens, last_cache_read_tokens,
                message_count, created_at, updated_at
            )
            SELECT
                lower(hex(randomblob(16))), chat_id, user_id, model_id,
                total_input_tokens, total_output_tokens, total_tokens, total_cache_read_tokens,
                last_input_tokens, last_output_tokens, last_cache_read_tokens,
                message_count, created_at, updated_at
            FROM temp_conv_agg
            """
        )
    )

    db.execute(
        text(
            """
            INSERT INTO daily_token_usage (
                id, user_id, date,
                total_input_tokens, total_output_tokens, total_tokens, total_cache_read_tokens,
                conversation_count, message_count, created_at, updated_at
            )
            SELECT
                lower(hex(randomblob(16))), user_id, date,
                total_input_tokens, total_output_tokens, total_tokens, total_cache_read_tokens,
                conversation_count, message_count, created_at, updated_at
            FROM temp_daily_agg
            """
        )
    )

    db.execute(
        text(
            """
            INSERT INTO model_token_usage (
                id, user_id, model_id,
                total_input_tokens, total_output_tokens, total_tokens, total_cache_read_tokens,
                conversation_count, message_count, created_at, updated_at
            )
            SELECT
                lower(hex(randomblob(16))), user_id, model_id,
                total_input_tokens, total_output_tokens, total_tokens, total_cache_read_tokens,
                conversation_count, message_count, created_at, updated_at
            FROM temp_model_user_agg
            UNION ALL
            SELECT
                lower(hex(randomblob(16))), NULL, model_id,
                total_input_tokens, total_output_tokens, total_tokens, total_cache_read_tokens,
                conversation_count, message_count, created_at, updated_at
            FROM temp_model_global_agg
            """
        )
    )

    db.commit()
    _print_summary(db, "written")
    log.info("Analytics rebuild complete.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild Open WebUI token analytics from raw and legacy usage")
    parser.add_argument("--dry-run", action="store_true", help="Compute and print totals without modifying analytics tables")
    parser.add_argument(
        "--raw-only",
        action="store_true",
        help="Ignore legacy aggregate-only conversation_token_usage rows. Not recommended if you used old subagents.",
    )
    args = parser.parse_args()

    with get_db() as db:
        if db.bind.dialect.name != "sqlite":
            raise RuntimeError(f"Only SQLite rebuild is implemented, got dialect={db.bind.dialect.name!r}")
        _rebuild(db, dry_run=args.dry_run, include_legacy_aggregates=not args.raw_only)


if __name__ == "__main__":
    main()
