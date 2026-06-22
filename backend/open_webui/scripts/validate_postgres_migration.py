#!/usr/bin/env python3
"""Validate an Open WebUI SQLite-to-Postgres migration.

Checks source SQLite row counts against target Postgres tables and validates
derived tables rebuilt during migration:
- group_user from group.user_ids
- chat_search from chat
- chat_message_search from chat_message
- document_chunk from Chroma embeddings when --chroma-dir is present
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

import asyncpg

LIVE_DATA_DIR = Path(
    "/home/tennisbowling/.local/lib/python3.12/site-packages/open_webui/data"
)
DEFAULT_SQLITE_DB = LIVE_DATA_DIR / "webui.db"
DEFAULT_CHROMA_DIR = LIVE_DATA_DIR / "vector_db"

SKIP_TABLE_PREFIXES = ("sqlite_", "chat_fts", "message_fts")
SKIP_TABLES = {
    "alembic_version",
    "embedding_fulltext_search",
    "embedding_fulltext_search_config",
    "embedding_fulltext_search_content",
    "embedding_fulltext_search_data",
    "embedding_fulltext_search_docsize",
    "embedding_fulltext_search_idx",
}


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _sqlite_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return [
        row[0]
        for row in rows
        if row[0] not in SKIP_TABLES
        and not any(row[0].startswith(prefix) for prefix in SKIP_TABLE_PREFIXES)
    ]


def _sqlite_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {_quote_ident(table)}").fetchone()[0])


async def _pg_tables(pg: asyncpg.Connection) -> set[str]:
    rows = await pg.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
    )
    return {row["tablename"] for row in rows}


async def _pg_count(pg: asyncpg.Connection, table: str) -> int:
    return int(await pg.fetchval(f"SELECT COUNT(*) FROM {_quote_ident(table)}"))


def _chroma_embedding_count(chroma_dir: Path) -> int | None:
    db_path = chroma_dir / "chroma.sqlite3"
    if not db_path.exists():
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return _sqlite_count(conn, "embeddings")
    finally:
        conn.close()


def _parse_json_maybe(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return None
    return None


def _legacy_extra_message_count(conn: sqlite3.Connection) -> int:
    try:
        existing = {
            (row[0], row[1])
            for row in conn.execute("SELECT chat_id, message_id FROM chat_message")
        }
    except Exception:
        existing = set()

    total = 0
    rows = conn.execute(
        "SELECT id, chat FROM chat WHERE COALESCE(messages_migrated, 0) = 0"
    ).fetchall()
    for chat_id, raw_chat in rows:
        chat_data = _parse_json_maybe(raw_chat) or {}
        if not isinstance(chat_data, dict):
            continue
        history = chat_data.get("history") if isinstance(chat_data.get("history"), dict) else {}
        messages = history.get("messages") if isinstance(history, dict) else None
        if not isinstance(messages, dict):
            messages = {
                str(msg.get("id") or idx): msg
                for idx, msg in enumerate(chat_data.get("messages") or [])
                if isinstance(msg, dict)
            }
        for mid, msg in messages.items():
            if isinstance(msg, dict) and (chat_id, str(mid)) not in existing:
                total += 1
                existing.add((chat_id, str(mid)))
    return total


async def validate(sqlite_db: Path, database_url: str, chroma_dir: Path | None) -> list[str]:
    sqlite_conn = sqlite3.connect(f"file:{sqlite_db}?mode=ro", uri=True)
    pg = await asyncpg.connect(database_url.replace("postgresql+asyncpg://", "postgresql://", 1))
    failures: list[str] = []
    try:
        target_tables = await _pg_tables(pg)
        legacy_extra_messages = _legacy_extra_message_count(sqlite_conn)
        for table in _sqlite_tables(sqlite_conn):
            if table not in target_tables:
                failures.append(f"missing target table: {table}")
                continue
            source_count = _sqlite_count(sqlite_conn, table)
            if table == "chat_message":
                source_count += legacy_extra_messages
            target_count = await _pg_count(pg, table)
            if source_count != target_count:
                failures.append(f"count mismatch {table}: sqlite={source_count} postgres={target_count}")

        chat_count = await _pg_count(pg, "chat")
        chat_search_count = await _pg_count(pg, "chat_search")
        if chat_search_count != chat_count:
            failures.append(f"chat_search mismatch: chat={chat_count} chat_search={chat_search_count}")

        msg_count = await _pg_count(pg, "chat_message")
        msg_search_count = await _pg_count(pg, "chat_message_search")
        if msg_search_count != msg_count:
            failures.append(
                f"chat_message_search mismatch: chat_message={msg_count} chat_message_search={msg_search_count}"
            )

        expected_group_users = await pg.fetchval(
            """
            SELECT COUNT(*)
            FROM "group" g
            CROSS JOIN LATERAL jsonb_array_elements_text(COALESCE(g.user_ids, '[]'::jsonb)) AS elem(value)
            """
        )
        group_user_count = await _pg_count(pg, "group_user")
        if int(expected_group_users or 0) != group_user_count:
            failures.append(
                f"group_user mismatch: expected={int(expected_group_users or 0)} group_user={group_user_count}"
            )

        if chroma_dir:
            chroma_count = _chroma_embedding_count(chroma_dir)
            if chroma_count is not None:
                pg_vectors = await _pg_count(pg, "document_chunk") if "document_chunk" in target_tables else 0
                if pg_vectors != chroma_count:
                    failures.append(f"document_chunk mismatch: chroma={chroma_count} postgres={pg_vectors}")
    finally:
        sqlite_conn.close()
        await pg.close()
    return failures


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite-db", type=Path, default=DEFAULT_SQLITE_DB)
    parser.add_argument("--chroma-dir", type=Path, default=DEFAULT_CHROMA_DIR)
    parser.add_argument("--skip-chroma", action="store_true")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    args = parser.parse_args()

    if not args.database_url:
        raise SystemExit("DATABASE_URL or --database-url is required")
    if not args.sqlite_db.exists():
        raise SystemExit(f"SQLite DB not found: {args.sqlite_db}")

    failures = await validate(
        args.sqlite_db,
        args.database_url,
        None if args.skip_chroma else args.chroma_dir,
    )
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        raise SystemExit(1)
    print("Postgres migration validation passed")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
