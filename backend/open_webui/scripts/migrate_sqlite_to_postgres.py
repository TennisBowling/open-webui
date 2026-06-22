#!/usr/bin/env python3
"""Offline SQLite/Chroma to Postgres migration.

This script is intentionally independent of the app runtime so it can read the
old SQLite files after the app is stopped and load an already-migrated Postgres
schema. It copies all non-FTS SQLite tables, creates missing legacy/custom
tables, forces remaining embedded chat messages into chat_message rows, and can
load Chroma collections into pgvector's document_chunk table.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pickle
import shutil
import sqlite3
import string
import sys
import tempfile
import uuid
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import asyncpg


LIVE_DATA_DIR = Path(
    "/home/tennisbowling/.local/lib/python3.12/site-packages/open_webui/data"
)
DEFAULT_SQLITE_DB = LIVE_DATA_DIR / "webui.db"
DEFAULT_CHROMA_DB_DIR = LIVE_DATA_DIR / "vector_db"

SKIP_TABLE_PREFIXES = (
    "sqlite_",
    "chat_fts",
    "message_fts",
)
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


def _sqlite_rows(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    rows = conn.execute(f"SELECT * FROM {_quote_ident(table)}").fetchall()
    return [dict(row) for row in rows]


def _sqlite_row_batches(
    conn: sqlite3.Connection, table: str, batch_size: int
) -> Iterable[list[dict[str, Any]]]:
    cursor = conn.execute(f"SELECT * FROM {_quote_ident(table)}")
    try:
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break
            yield [dict(row) for row in rows]
    finally:
        cursor.close()


def _sqlite_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = [row[0] for row in rows]
    return [
        name
        for name in names
        if name not in SKIP_TABLES
        and not any(name.startswith(prefix) for prefix in SKIP_TABLE_PREFIXES)
    ]


def _sqlite_columns(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    return conn.execute(f"PRAGMA table_info({_quote_ident(table)})").fetchall()


def _map_sqlite_type(raw_type: str) -> str:
    typ = (raw_type or "").upper()
    if "UUID" in typ:
        return "UUID"
    if "BIGINT" in typ:
        return "BIGINT"
    if "INT" in typ:
        return "INTEGER"
    if "REAL" in typ or "FLOA" in typ or "DOUB" in typ:
        return "DOUBLE PRECISION"
    if "BOOL" in typ:
        return "BOOLEAN"
    if "DATE" in typ or "TIME" in typ:
        return "TIMESTAMP"
    if "JSON" in typ:
        return "JSONB"
    return "TEXT"


async def _target_tables(pg: asyncpg.Connection) -> set[str]:
    rows = await pg.fetch(
        """
        SELECT tablename
        FROM pg_tables
        WHERE schemaname = current_schema()
        """
    )
    return {row["tablename"] for row in rows}


async def _target_column_types(pg: asyncpg.Connection, table: str) -> dict[str, str]:
    rows = await pg.fetch(
        """
        SELECT column_name, udt_name
        FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = $1
        ORDER BY ordinal_position
        """,
        table,
    )
    return {row["column_name"]: row["udt_name"] for row in rows}


async def _ensure_legacy_table(
    pg: asyncpg.Connection, sqlite_conn: sqlite3.Connection, table: str
) -> None:
    columns = _sqlite_columns(sqlite_conn, table)
    if not columns:
        return
    pk_cols = [row["name"] for row in columns if row["pk"]]
    column_sql = []
    for col in columns:
        name = col["name"]
        pg_type = _map_sqlite_type(col["type"])
        nullable = " NOT NULL" if col["notnull"] else ""
        column_sql.append(f"{_quote_ident(name)} {pg_type}{nullable}")
    if pk_cols:
        pk = ", ".join(_quote_ident(col) for col in pk_cols)
        column_sql.append(f"PRIMARY KEY ({pk})")
    await pg.execute(
        f"CREATE TABLE IF NOT EXISTS {_quote_ident(table)} ({', '.join(column_sql)})"
    )


async def _ensure_legacy_constraints(pg: asyncpg.Connection, table: str) -> None:
    if table != "token_tracking_credit_group_user":
        return
    await pg.execute(
        """
        DELETE FROM token_tracking_credit_group_user a
        USING token_tracking_credit_group_user b
        WHERE a.ctid < b.ctid
          AND a.credit_group_id = b.credit_group_id
          AND a.user_id = b.user_id
        """
    )
    await pg.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS token_tracking_credit_group_user_uidx
        ON token_tracking_credit_group_user (credit_group_id, user_id)
        """
    )


def _parse_json_maybe(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        current = value.strip()
        if current == "":
            return None
        for _ in range(2):
            try:
                parsed = json.loads(current)
            except Exception:
                try:
                    parsed = json.loads(_repair_json_escapes(current))
                except Exception:
                    return value
            if isinstance(parsed, str) and parsed.strip().startswith(("{", "[")):
                current = parsed.strip()
                continue
            return parsed
        return parsed
    return value


_JSON_SIMPLE_ESCAPES = set('"\\/bfnrt')
_HEX_DIGITS = set(string.hexdigits)


def _repair_json_escapes(value: str) -> str:
    out: list[str] = []
    i = 0
    n = len(value)
    while i < n:
        char = value[i]
        if char != "\\":
            out.append(char)
            i += 1
            continue
        if i + 1 >= n:
            out.append("\\\\")
            i += 1
        elif value[i + 1] in _JSON_SIMPLE_ESCAPES:
            out.append("\\")
            out.append(value[i + 1])
            i += 2
        elif (
            value[i + 1] == "u"
            and i + 5 < n
            and all(char in _HEX_DIGITS for char in value[i + 2 : i + 6])
        ):
            out.append(value[i : i + 6])
            i += 6
        else:
            out.append("\\\\")
            i += 1
    return "".join(out)


def _strip_pg_nuls(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\x00", "").replace("\\u0000", "")
    if isinstance(value, list):
        return [_strip_pg_nuls(item) for item in value]
    if isinstance(value, dict):
        return {
            _strip_pg_nuls(key): _strip_pg_nuls(item)
            for key, item in value.items()
        }
    return value


def _parse_temporal(value: Any, udt: str) -> Any:
    if value is None or isinstance(value, (datetime, date)):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
            return parsed.date() if udt == "date" else parsed
        except Exception:
            return value
    return value


def _extract_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    return ""


def _split_message_for_table(chat_id: str, seq: int, mid: str, msg: dict[str, Any]) -> dict:
    content = msg.get("content", "")
    content_is_json = 0
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
        content_is_json = 1

    timestamp = msg.get("timestamp")
    try:
        timestamp = int(timestamp) if timestamp is not None else None
    except (TypeError, ValueError):
        timestamp = None

    meta = {
        key: value
        for key, value in msg.items()
        if key
        not in {
            "id",
            "parentId",
            "role",
            "content",
            "model",
            "timestamp",
            "statusHistory",
        }
    }

    return {
        "chat_id": chat_id,
        "message_id": str(mid),
        "parent_id": msg.get("parentId"),
        "role": msg.get("role"),
        "content": content,
        "content_is_json": content_is_json,
        "model": msg.get("model"),
        "timestamp": timestamp,
        "sequence": seq,
        "status_history": msg.get("statusHistory"),
        "meta": meta or None,
    }


def _legacy_messages(chat_row: dict[str, Any]) -> tuple[dict[str, dict], dict[str, Any]]:
    chat_id = chat_row["id"]
    chat_data = _parse_json_maybe(chat_row.get("chat")) or {}
    if not isinstance(chat_data, dict):
        raise ValueError(f"chat {chat_id} has non-object chat JSON")
    history = chat_data.get("history") if isinstance(chat_data.get("history"), dict) else {}
    messages = history.get("messages") if isinstance(history, dict) else None
    if not isinstance(messages, dict):
        messages = {
            str(msg.get("id") or idx): msg
            for idx, msg in enumerate(chat_data.get("messages") or [])
            if isinstance(msg, dict)
        }
    return messages, chat_data


def _strip_embedded_messages(chat_data: dict[str, Any]) -> dict[str, Any]:
    out = dict(chat_data)
    history = out.get("history")
    if isinstance(history, dict) and "messages" in history:
        history = dict(history)
        history.pop("messages", None)
        out["history"] = history
    out.pop("messages", None)
    return out


def _prepare_rows(
    table: str,
    rows: list[dict[str, Any]],
    target_types: dict[str, str],
) -> list[dict[str, Any]]:
    prepared = []
    for row in rows:
        out = {}
        for key, value in row.items():
            udt = target_types.get(key)
            value = _strip_pg_nuls(value)
            if udt in {"json", "jsonb"}:
                parsed = _strip_pg_nuls(_parse_json_maybe(value))
                out[key] = None if parsed is None else json.dumps(parsed, ensure_ascii=False)
            elif udt == "bool":
                out[key] = None if value is None else bool(value)
            elif udt == "uuid" and isinstance(value, str):
                out[key] = uuid.UUID(value)
            elif udt in {"timestamp", "timestamptz", "date"}:
                out[key] = _parse_temporal(value, udt)
            else:
                out[key] = value
        prepared.append(out)
    return prepared


async def _copy_records(
    pg: asyncpg.Connection,
    table: str,
    rows: list[dict[str, Any]],
    target_types: dict[str, str],
    batch_size: int,
) -> int:
    if not rows:
        return 0
    columns = [col for col in rows[0].keys() if col in target_types]
    if not columns:
        return 0
    names = ", ".join(_quote_ident(col) for col in columns)
    args = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
    conflict = " ON CONFLICT DO NOTHING"
    sql = f"INSERT INTO {_quote_ident(table)} ({names}) VALUES ({args}){conflict}"
    prepared = _prepare_rows(table, rows, target_types)
    total = 0
    for start in range(0, len(prepared), batch_size):
        batch = prepared[start : start + batch_size]
        values = [[row.get(col) for col in columns] for row in batch]
        await pg.executemany(sql, values)
        total += len(batch)
    return total


async def _rebuild_chat_search(pg: asyncpg.Connection) -> None:
    await pg.execute("TRUNCATE TABLE chat_search")
    await pg.execute(
        """
        INSERT INTO chat_search (chat_id, title, body)
        SELECT
            c.id,
            COALESCE(c.title, ''),
            LEFT(
                COALESCE(c.title, '') || ' ' ||
                COALESCE(string_agg(
                    regexp_replace(
                        COALESCE(cm.content, ''),
                        'data:image/[a-zA-Z0-9.+/_-]*;base64,[A-Za-z0-9+/=]+',
                        '[image]',
                        'g'
                    ),
                    ' ' ORDER BY cm.sequence
                ), ''),
                65536
            ) AS body
        FROM chat c
        LEFT JOIN chat_message cm ON cm.chat_id = c.id
        GROUP BY c.id, c.title
        ON CONFLICT (chat_id) DO UPDATE
        SET title = EXCLUDED.title, body = EXCLUDED.body
        """
    )
    await pg.execute("TRUNCATE TABLE chat_message_search")
    await pg.execute(
        """
        INSERT INTO chat_message_search (chat_id, message_id, role, content)
        SELECT chat_id, message_id, role, LEFT(
            regexp_replace(
                COALESCE(content, ''),
                'data:image/[a-zA-Z0-9.+/_-]*;base64,[A-Za-z0-9+/=]+',
                '[image]',
                'g'
            ),
            65536
        )
        FROM chat_message
        ON CONFLICT (chat_id, message_id) DO UPDATE SET
            role = EXCLUDED.role,
            content = EXCLUDED.content
        """
    )


async def _rebuild_group_users(pg: asyncpg.Connection) -> None:
    await pg.execute("TRUNCATE TABLE group_user")
    await pg.execute(
        """
        INSERT INTO group_user (group_id, user_id, created_at)
        SELECT g.id, elem.value, EXTRACT(EPOCH FROM now())::BIGINT
        FROM "group" g
        CROSS JOIN LATERAL jsonb_array_elements_text(COALESCE(g.user_ids, '[]'::jsonb)) AS elem(value)
        ON CONFLICT DO NOTHING
        """
    )


async def migrate_app_db(
    sqlite_path: Path, database_url: str, batch_size: int, dry_run: bool
) -> None:
    sqlite_conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    sqlite_conn.row_factory = sqlite3.Row
    sqlite_conn.execute("PRAGMA query_only=ON")
    sqlite_conn.execute("BEGIN")
    pg = await asyncpg.connect(database_url)
    try:
        tables = _sqlite_tables(sqlite_conn)
        priority = {"user": 0}
        tables.sort(key=lambda table: (priority.get(table, 10), table))
        target_tables = await _target_tables(pg)
        existing_chat_messages = {
            (row["chat_id"], row["message_id"])
            for row in _sqlite_rows(sqlite_conn, "chat_message")
        }
        extra_chat_messages: list[dict[str, Any]] = []

        for table in tables:
            if table not in target_tables:
                await _ensure_legacy_table(pg, sqlite_conn, table)
                target_tables = await _target_tables(pg)
            await _ensure_legacy_constraints(pg, table)
            target_types = await _target_column_types(pg, table)
            source_count = 0
            inserted_count = 0
            for rows in _sqlite_row_batches(sqlite_conn, table, batch_size):
                source_count += len(rows)

                if table == "chat":
                    for row in rows:
                        if int(row.get("messages_migrated") or 0) != 0:
                            continue
                        messages, chat_data = _legacy_messages(row)
                        if not messages:
                            raise ValueError(
                                f"legacy chat {row['id']} has no extractable messages"
                            )
                        for seq, (mid, msg) in enumerate(messages.items()):
                            if not isinstance(msg, dict):
                                continue
                            key = (row["id"], str(mid))
                            if key not in existing_chat_messages:
                                extra_chat_messages.append(
                                    _split_message_for_table(row["id"], seq, str(mid), msg)
                                )
                                existing_chat_messages.add(key)
                        row["messages_migrated"] = 1
                        row["chat"] = _strip_embedded_messages(chat_data)

                if not dry_run:
                    inserted_count += await _copy_records(
                        pg, table, rows, target_types, batch_size
                    )
            print(f"{table}: {source_count} source rows, {inserted_count} inserted")

        if extra_chat_messages:
            target_types = await _target_column_types(pg, "chat_message")
            count = 0 if dry_run else await _copy_records(
                pg, "chat_message", extra_chat_messages, target_types, batch_size
            )
            print(f"chat_message legacy extras: {len(extra_chat_messages)} source rows, {count} inserted")

        if not dry_run:
            await _rebuild_group_users(pg)
            print("group_user rebuilt")
            await _rebuild_chat_search(pg)
            print("chat_search rebuilt")
    finally:
        try:
            sqlite_conn.execute("ROLLBACK")
        except Exception:
            pass
        await pg.close()
        sqlite_conn.close()


def _vector_literal(vector: Iterable[float | int]) -> str:
    return "[" + ",".join(str(float(v)) for v in vector) + "]"


async def migrate_chroma(chroma_dir: Path, database_url: str, dry_run: bool) -> None:
    import chromadb

    compat_dir: Path | None = None
    try:
        client = chromadb.PersistentClient(path=str(chroma_dir))
        collections = client.list_collections()
    except KeyError as exc:
        if exc.args != ("_type",):
            raise
        compat_dir = _prepare_chroma_compat_dir(chroma_dir)
        client = chromadb.PersistentClient(path=str(compat_dir))
        collections = client.list_collections()
    pg = await asyncpg.connect(database_url)
    try:
        await pg.execute("CREATE EXTENSION IF NOT EXISTS vector")
        dims: set[int] = set()
        payloads = []
        for collection in collections:
            collection_name = _chroma_collection_name(collection)
            coll = client.get_collection(collection_name)
            offset = 0
            while True:
                result = coll.get(
                    include=["embeddings", "documents", "metadatas"],
                    limit=1000,
                    offset=offset,
                )
                ids = result.get("ids") or []
                if not ids:
                    break
                docs = result.get("documents")
                metadatas = result.get("metadatas")
                embeddings = result.get("embeddings")
                docs = docs if docs is not None else []
                metadatas = metadatas if metadatas is not None else []
                embeddings = embeddings if embeddings is not None else []
                for idx, item_id in enumerate(ids):
                    vector = list(embeddings[idx])
                    dims.add(len(vector))
                    payloads.append(
                        (
                            str(item_id),
                            collection_name,
                            _strip_pg_nuls(docs[idx]) if idx < len(docs) else None,
                            json.dumps(_strip_pg_nuls(metadatas[idx]), ensure_ascii=False)
                            if idx < len(metadatas) and metadatas[idx] is not None
                            else None,
                            _vector_literal(vector),
                        )
                    )
                offset += len(ids)
        if not payloads:
            print("No Chroma embeddings found")
            return
        if len(dims) != 1:
            raise ValueError(f"Chroma embeddings have mixed dimensions: {sorted(dims)}")
        dim = dims.pop()
        await pg.execute(
            f"""
            CREATE TABLE IF NOT EXISTS document_chunk (
                id TEXT PRIMARY KEY,
                collection_name TEXT NOT NULL,
                text TEXT,
                vmetadata JSONB,
                vector vector({dim})
            )
            """
        )
        await pg.execute(
            "CREATE INDEX IF NOT EXISTS idx_document_chunk_collection_name "
            "ON document_chunk (collection_name)"
        )
        if dry_run:
            print(f"Chroma dry-run: {len(payloads)} embeddings across {len(collections)} collections")
            return
        await pg.executemany(
            """
            INSERT INTO document_chunk (id, collection_name, text, vmetadata, vector)
            VALUES ($1, $2, $3, $4, $5::vector)
            ON CONFLICT (id) DO UPDATE SET
                collection_name = EXCLUDED.collection_name,
                text = EXCLUDED.text,
                vmetadata = EXCLUDED.vmetadata,
                vector = EXCLUDED.vector
            """,
            payloads,
        )
        await pg.execute(
            "CREATE INDEX IF NOT EXISTS idx_document_chunk_vector "
            "ON document_chunk USING ivfflat (vector vector_cosine_ops) WITH (lists = 100)"
        )
        print(f"Chroma migrated: {len(payloads)} embeddings, dimension={dim}")
    finally:
        await pg.close()
        if compat_dir is not None:
            shutil.rmtree(compat_dir, ignore_errors=True)


def _chroma_collection_name(collection: Any) -> str:
    if isinstance(collection, str):
        return collection
    try:
        name = collection.name
        if name:
            return str(name)
    except Exception:
        pass
    return str(collection)


def _prepare_chroma_compat_dir(chroma_dir: Path) -> Path:
    from chromadb.segment.impl.vector.local_persistent_hnsw import PersistentData

    source_db = chroma_dir / "chroma.sqlite3"
    if not source_db.exists():
        raise FileNotFoundError(source_db)

    tmp = Path(tempfile.mkdtemp(prefix="openwebui-chroma-compat-"))
    shutil.copy2(source_db, tmp / "chroma.sqlite3")

    conn = sqlite3.connect(tmp / "chroma.sqlite3")
    try:
        conn.executescript(
            """
            UPDATE collections
            SET config_json_str = json_object(
              '_type', 'CollectionConfigurationInternal',
              'hnsw_configuration', json_object(
                '_type', 'HNSWConfigurationInternal',
                'space', COALESCE(json_extract(config_json_str, '$.vector_index.hnsw.space'), 'l2'),
                'ef_construction', COALESCE(json_extract(config_json_str, '$.vector_index.hnsw.ef_construction'), 100),
                'ef_search', COALESCE(json_extract(config_json_str, '$.vector_index.hnsw.ef_search'), 100),
                'num_threads', 24,
                'M', COALESCE(json_extract(config_json_str, '$.vector_index.hnsw.max_neighbors'), 16),
                'resize_factor', COALESCE(json_extract(config_json_str, '$.vector_index.hnsw.resize_factor'), 1.2),
                'batch_size', 100,
                'sync_threshold', COALESCE(json_extract(config_json_str, '$.vector_index.hnsw.sync_threshold'), 1000)
              )
            )
            WHERE json_extract(config_json_str, '$._type') IS NULL;
            """
        )
        dimensions = {
            row[0]: row[1]
            for row in conn.execute(
                """
                SELECT s.id, c.dimension
                FROM segments s
                JOIN collections c ON c.id = s.collection
                WHERE s.scope = 'VECTOR'
                """
            ).fetchall()
        }
        conn.commit()
    finally:
        conn.close()

    for child in chroma_dir.iterdir():
        if child.name == "chroma.sqlite3":
            continue
        metadata_pickle = child / "index_metadata.pickle"
        if child.is_dir() and metadata_pickle.exists():
            with open(metadata_pickle, "rb") as f:
                metadata = pickle.load(f)
            if isinstance(metadata, dict):
                dest = tmp / child.name
                dest.mkdir()
                for grandchild in child.iterdir():
                    if grandchild.name == "index_metadata.pickle":
                        continue
                    os.symlink(
                        grandchild,
                        dest / grandchild.name,
                        target_is_directory=grandchild.is_dir(),
                    )
                converted = PersistentData(
                    dimensionality=metadata.get("dimensionality") or dimensions.get(child.name),
                    total_elements_added=int(metadata.get("total_elements_added") or 0),
                    id_to_label=dict(metadata.get("id_to_label") or {}),
                    label_to_id=dict(metadata.get("label_to_id") or {}),
                    id_to_seq_id=dict(metadata.get("id_to_seq_id") or {}),
                )
                with open(dest / "index_metadata.pickle", "wb") as f:
                    pickle.dump(converted, f)
                continue
        os.symlink(child, tmp / child.name, target_is_directory=child.is_dir())
    return tmp


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite-db", type=Path, default=DEFAULT_SQLITE_DB)
    parser.add_argument("--chroma-dir", type=Path, default=DEFAULT_CHROMA_DB_DIR)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--skip-chroma", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.database_url:
        raise SystemExit("DATABASE_URL or --database-url is required")
    database_url = args.database_url.replace("postgresql+asyncpg://", "postgresql://")
    if not args.sqlite_db.exists():
        raise SystemExit(f"SQLite DB not found: {args.sqlite_db}")

    await migrate_app_db(args.sqlite_db, database_url, args.batch_size, args.dry_run)
    if not args.skip_chroma:
        if not args.chroma_dir.exists():
            raise SystemExit(f"Chroma directory not found: {args.chroma_dir}")
        await migrate_chroma(args.chroma_dir, database_url, args.dry_run)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
