#!/usr/bin/env python3
"""Backfill cached prompt token analytics on PostgreSQL."""

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


async def _backfill(dry_run: bool = False) -> None:
    async with get_db() as db:
        await db.execute(text("DROP TABLE IF EXISTS temp_cache_usage_events"))
        await db.execute(
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
                        NULLIF(cm.meta->>'selectedModelId', ''),
                        NULLIF(c.model_id_primary, ''),
                        ''
                    ) AS model_id,
                    COALESCE(NULLIF(cm.meta #>> '{usage,prompt_tokens_details,cached_tokens}', '')::bigint, 0) AS cache_read_tokens,
                    COALESCE(cm.timestamp, c.updated_at, c.created_at, 0) AS event_ts,
                    cm.sequence AS sequence
                FROM chat_message cm
                JOIN chat c ON c.id = cm.chat_id
                WHERE c.user_id NOT LIKE 'shared-%'
                  AND cm.meta IS NOT NULL
                  AND jsonb_typeof(cm.meta->'usage') = 'object'
                """
            )
        )
        await db.execute(text("CREATE INDEX temp_cache_usage_chat_idx ON temp_cache_usage_events(attributed_chat_id)"))
        await db.execute(text("CREATE INDEX temp_cache_usage_daily_idx ON temp_cache_usage_events(user_id, event_ts)"))
        await db.execute(text("CREATE INDEX temp_cache_usage_model_idx ON temp_cache_usage_events(user_id, model_id)"))

        summary = (
            await db.execute(
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
            )
        ).mappings().first()
        log.info("Cache-read source summary: %s", dict(summary or {}))

        if dry_run:
            await db.rollback()
            log.info("Dry run complete; no rows updated.")
            return

        await db.execute(
            text(
                """
                UPDATE conversation_token_usage ctu
                SET
                    total_cache_read_tokens = COALESCE(src.total_cache, 0),
                    last_cache_read_tokens = COALESCE(last_evt.cache_read_tokens, 0)
                FROM (
                    SELECT attributed_chat_id, SUM(cache_read_tokens) AS total_cache
                    FROM temp_cache_usage_events GROUP BY attributed_chat_id
                ) src
                LEFT JOIN LATERAL (
                    SELECT e.cache_read_tokens
                    FROM temp_cache_usage_events e
                    WHERE e.attributed_chat_id = src.attributed_chat_id
                    ORDER BY e.event_ts DESC, e.sequence DESC, e.message_id DESC
                    LIMIT 1
                ) last_evt ON true
                WHERE ctu.chat_id = src.attributed_chat_id
                """
            )
        )

        await db.execute(
            text(
                """
                UPDATE daily_token_usage dtu
                SET total_cache_read_tokens = COALESCE(src.total_cache, 0)
                FROM (
                    SELECT
                        user_id,
                        to_char(to_timestamp(event_ts) AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS date,
                        SUM(cache_read_tokens) AS total_cache
                    FROM temp_cache_usage_events
                    GROUP BY user_id, to_char(to_timestamp(event_ts) AT TIME ZONE 'UTC', 'YYYY-MM-DD')
                ) src
                WHERE dtu.user_id = src.user_id AND dtu.date = src.date
                """
            )
        )

        await db.execute(
            text(
                """
                UPDATE model_token_usage mtu
                SET total_cache_read_tokens = COALESCE(src.total_cache, 0)
                FROM (
                    SELECT user_id, model_id, SUM(cache_read_tokens) AS total_cache
                    FROM temp_cache_usage_events
                    GROUP BY user_id, model_id
                    UNION ALL
                    SELECT NULL AS user_id, model_id, SUM(cache_read_tokens) AS total_cache
                    FROM temp_cache_usage_events
                    GROUP BY model_id
                ) src
                WHERE mtu.model_id = src.model_id
                  AND ((mtu.user_id IS NULL AND src.user_id IS NULL) OR mtu.user_id = src.user_id)
                """
            )
        )
        await db.commit()
        log.info("Cache-read backfill complete")


async def amain() -> None:
    parser = argparse.ArgumentParser(description="Backfill cache-read token analytics on PostgreSQL")
    parser.add_argument("--dry-run", action="store_true", help="Inspect counts without updating tables")
    args = parser.parse_args()
    await _backfill(dry_run=args.dry_run)


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
