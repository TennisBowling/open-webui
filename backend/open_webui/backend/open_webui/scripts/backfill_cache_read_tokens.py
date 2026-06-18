#!/usr/bin/env python3
"""Backfill cached prompt token analytics from persisted message usage.

Reads per-request usage payloads stored in ``chat_message.meta`` and aggregates
``usage.prompt_tokens_details.cached_tokens`` into the analytics tables:

- conversation_token_usage.total_cache_read_tokens
- conversation_token_usage.last_cache_read_tokens
- daily_token_usage.total_cache_read_tokens
- model_token_usage.total_cache_read_tokens

This intentionally does not rewrite historical prompt/output/total token fields.
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


def _backfill_sqlite(db, dry_run: bool = False) -> None:
    _ensure_columns(db)

    db.execute(text("DROP TABLE IF EXISTS temp_cache_usage_events"))
    db.execute(
        text(
            """
            CREATE TEMP TABLE temp_cache_usage_events AS
            SELECT
                COALESCE(NULLIF(c.subagent_of, ''), cm.chat_id) AS attributed_chat_id,
                cm.chat_id AS source_chat_id,
                cm.message_id AS message_id,
                c.user_id AS user_id,
                COALESCE(
                    NULLIF(cm.model, ''),
                    NULLIF(json_extract(cm.meta, '$.selectedModelId'), ''),
                    NULLIF(c.model_id_primary, ''),
                    ''
                ) AS model_id,
                CAST(COALESCE(json_extract(cm.meta, '$.usage.prompt_tokens_details.cached_tokens'), 0) AS INTEGER) AS cache_read_tokens,
                COALESCE(cm.timestamp, c.updated_at, c.created_at, 0) AS event_ts,
                cm.sequence AS sequence
            FROM chat_message cm
            JOIN chat c ON c.id = cm.chat_id
            WHERE c.user_id NOT LIKE 'shared-%'
              AND json_valid(cm.meta) = 1
              AND json_type(cm.meta, '$.usage') IS NOT NULL
            """
        )
    )
    db.execute(text("CREATE INDEX temp_cache_usage_chat_idx ON temp_cache_usage_events(attributed_chat_id)"))
    db.execute(text("CREATE INDEX temp_cache_usage_daily_idx ON temp_cache_usage_events(user_id, event_ts)"))
    db.execute(text("CREATE INDEX temp_cache_usage_model_idx ON temp_cache_usage_events(user_id, model_id)"))

    summary = db.execute(
        text(
            """
            SELECT
                COUNT(*) AS usage_events,
                SUM(CASE WHEN cache_read_tokens > 0 THEN 1 ELSE 0 END) AS cached_events,
                COALESCE(SUM(cache_read_tokens), 0) AS total_cached,
                COUNT(DISTINCT attributed_chat_id) AS chats
            FROM temp_cache_usage_events
            """
        )
    ).mappings().first()

    log.info(
        "Found %(usage_events)s usage events (%(cached_events)s with cache reads), "
        "%(total_cached)s cached tokens across %(chats)s chats",
        dict(summary or {}),
    )

    if dry_run:
        db.rollback()
        log.info("Dry run complete; no rows updated.")
        return

    # Per-chat aggregate + latest request cache read.
    db.execute(
        text(
            """
            UPDATE conversation_token_usage
            SET
                total_cache_read_tokens = COALESCE((
                    SELECT SUM(e.cache_read_tokens)
                    FROM temp_cache_usage_events e
                    WHERE e.attributed_chat_id = conversation_token_usage.chat_id
                ), 0),
                last_cache_read_tokens = COALESCE((
                    SELECT e.cache_read_tokens
                    FROM temp_cache_usage_events e
                    WHERE e.attributed_chat_id = conversation_token_usage.chat_id
                    ORDER BY e.event_ts DESC, e.sequence DESC, e.message_id DESC
                    LIMIT 1
                ), 0)
            """
        )
    )

    # Per-user/day aggregate, using the message/request timestamp in UTC.
    db.execute(
        text(
            """
            UPDATE daily_token_usage
            SET total_cache_read_tokens = COALESCE((
                SELECT SUM(e.cache_read_tokens)
                FROM temp_cache_usage_events e
                WHERE e.user_id = daily_token_usage.user_id
                  AND date(e.event_ts, 'unixepoch') = daily_token_usage.date
            ), 0)
            """
        )
    )

    # Per-user/model and global model aggregates. user_id NULL means global.
    db.execute(
        text(
            """
            UPDATE model_token_usage
            SET total_cache_read_tokens = COALESCE((
                SELECT SUM(e.cache_read_tokens)
                FROM temp_cache_usage_events e
                WHERE e.model_id = model_token_usage.model_id
                  AND (
                      model_token_usage.user_id IS NULL
                      OR e.user_id = model_token_usage.user_id
                  )
            ), 0)
            """
        )
    )

    db.commit()

    post = db.execute(
        text(
            """
            SELECT
                (SELECT COALESCE(SUM(total_cache_read_tokens), 0) FROM conversation_token_usage) AS conversation_cached,
                (SELECT COALESCE(SUM(total_cache_read_tokens), 0) FROM daily_token_usage) AS daily_cached,
                (SELECT COALESCE(SUM(total_cache_read_tokens), 0) FROM model_token_usage WHERE user_id IS NULL) AS global_model_cached
            """
        )
    ).mappings().first()
    log.info("Backfill complete: %s", dict(post or {}))


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill cache-read token analytics")
    parser.add_argument("--dry-run", action="store_true", help="Inspect counts without updating tables")
    args = parser.parse_args()

    with get_db() as db:
        dialect = db.bind.dialect.name
        if dialect != "sqlite":
            raise RuntimeError(f"Only SQLite backfill is implemented, got dialect={dialect!r}")
        _backfill_sqlite(db, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
