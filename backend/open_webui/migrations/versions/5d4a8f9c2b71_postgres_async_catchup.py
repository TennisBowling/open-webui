"""Postgres async catch-up schema

Revision ID: 5d4a8f9c2b71
Revises: 4f6a2c8d9e10
Create Date: 2026-06-18

Creates the Postgres-only objects that earlier performance migrations skipped
when the dialect was not SQLite.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "5d4a8f9c2b71"
down_revision: Union[str, Sequence[str], None] = "4f6a2c8d9e10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


JSONB_COLUMNS = (
    ("channel", "data"),
    ("channel", "meta"),
    ("channel", "access_control"),
    ("chat", "chat"),
    ("chat", "meta"),
    ("config", "data"),
    ("feedback", "data"),
    ("feedback", "meta"),
    ("feedback", "snapshot"),
    ("file", "meta"),
    ("file", "data"),
    ("file", "access_control"),
    ("folder", "items"),
    ("folder", "meta"),
    ("folder", "data"),
    ("group", "data"),
    ("group", "meta"),
    ("group", "permissions"),
    ("group", "user_ids"),
    ("knowledge", "data"),
    ("knowledge", "meta"),
    ("knowledge", "access_control"),
    ("message", "data"),
    ("message", "meta"),
    ("model", "meta"),
    ("model", "params"),
    ("model", "access_control"),
    ("note", "data"),
    ("note", "meta"),
    ("note", "access_control"),
    ("prompt", "access_control"),
    ("tag", "meta"),
    ("token_group", "models"),
    ("token_usage_event", "raw_usage"),
    ("tool", "specs"),
    ("tool", "meta"),
    ("tool", "valves"),
    ("tool", "access_control"),
    ("user", "settings"),
    ("user", "info"),
)


def _jsonb_upgrade_sql(table: str, column: str) -> sa.TextClause:
    return sa.text(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = '{table}'
                  AND column_name = '{column}'
                  AND udt_name <> 'jsonb'
            ) THEN
                EXECUTE 'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE jsonb '
                    || 'USING CASE WHEN "{column}" IS NULL THEN NULL ELSE "{column}"::jsonb END';
            END IF;
        END $$;
        """
    )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("The Postgres-only runtime only supports PostgreSQL migrations")

    bind.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
    bind.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))

    for table, column in JSONB_COLUMNS:
        bind.execute(_jsonb_upgrade_sql(table, column))

    bind.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS chat_message (
                chat_id TEXT NOT NULL REFERENCES chat(id) ON DELETE CASCADE,
                message_id TEXT NOT NULL,
                parent_id TEXT,
                role TEXT,
                content TEXT,
                content_is_json INTEGER DEFAULT 0,
                model TEXT,
                timestamp BIGINT,
                sequence INTEGER NOT NULL,
                status_history JSONB,
                meta JSONB,
                PRIMARY KEY (chat_id, message_id)
            )
            """
        )
    )

    bind.execute(
        sa.text(
            "ALTER TABLE chat ADD COLUMN IF NOT EXISTS messages_migrated INTEGER NOT NULL DEFAULT 0"
        )
    )
    bind.execute(sa.text("ALTER TABLE chat ADD COLUMN IF NOT EXISTS subagent_of TEXT"))
    bind.execute(sa.text("ALTER TABLE chat ADD COLUMN IF NOT EXISTS model_id_primary TEXT"))
    bind.execute(sa.text("ALTER TABLE chat ADD COLUMN IF NOT EXISTS search_text TEXT"))

    bind.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS chat_message_chat_seq_idx "
            "ON chat_message (chat_id, sequence)"
        )
    )
    bind.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS chat_message_chat_parent_idx "
            "ON chat_message (chat_id, parent_id)"
        )
    )
    bind.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS user_id_updated_at_idx ON chat (user_id, updated_at DESC)"
        )
    )
    bind.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS chat_sidebar_default_idx "
            "ON chat (user_id, updated_at DESC) "
            "WHERE archived = false AND folder_id IS NULL "
            "AND (pinned = false OR pinned IS NULL) AND subagent_of IS NULL"
        )
    )
    bind.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS folder_id_user_id_idx ON chat (folder_id, user_id)"
        )
    )
    bind.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS group_user_ids_gin_idx "
            "ON \"group\" USING GIN (user_ids jsonb_path_ops)"
        )
    )
    bind.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS group_user (
                group_id TEXT NOT NULL REFERENCES "group"(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
                created_at BIGINT,
                PRIMARY KEY (group_id, user_id)
            )
            """
        )
    )
    bind.execute(
        sa.text("CREATE INDEX IF NOT EXISTS group_user_user_id_idx ON group_user (user_id)")
    )
    bind.execute(
        sa.text(
            """
            INSERT INTO group_user (group_id, user_id, created_at)
            SELECT g.id, elem.value, EXTRACT(EPOCH FROM now())::BIGINT
            FROM "group" g
            CROSS JOIN LATERAL jsonb_array_elements_text(COALESCE(g.user_ids, '[]'::jsonb)) AS elem(value)
            ON CONFLICT DO NOTHING
            """
        )
    )

    bind.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS chat_search (
                chat_id TEXT PRIMARY KEY REFERENCES chat(id) ON DELETE CASCADE,
                title TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '',
                search_vector tsvector GENERATED ALWAYS AS (
                    setweight(to_tsvector('simple', coalesce(title, '')), 'A') ||
                    setweight(to_tsvector('simple', coalesce(body, '')), 'B')
                ) STORED
            )
            """
        )
    )
    bind.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS chat_search_vector_idx "
            "ON chat_search USING GIN (search_vector)"
        )
    )
    bind.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS chat_search_title_trgm_idx "
            "ON chat_search USING GIN (title gin_trgm_ops)"
        )
    )
    bind.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS chat_search_body_trgm_idx "
            "ON chat_search USING GIN (body gin_trgm_ops)"
        )
    )
    bind.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS chat_message_search (
                chat_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                role TEXT,
                content TEXT NOT NULL DEFAULT '',
                search_vector tsvector GENERATED ALWAYS AS (
                    to_tsvector('simple', coalesce(content, ''))
                ) STORED,
                PRIMARY KEY (chat_id, message_id),
                FOREIGN KEY (chat_id, message_id)
                    REFERENCES chat_message(chat_id, message_id)
                    ON DELETE CASCADE
            )
            """
        )
    )
    bind.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS chat_message_search_vector_idx "
            "ON chat_message_search USING GIN (search_vector)"
        )
    )
    bind.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS chat_message_search_content_trgm_idx "
            "ON chat_message_search USING GIN (content gin_trgm_ops)"
        )
    )
    bind.execute(
        sa.text(
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
    )
    bind.execute(
        sa.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS model_token_usage_user_model_not_null_idx "
            "ON model_token_usage (user_id, model_id) WHERE user_id IS NOT NULL"
        )
    )
    bind.execute(
        sa.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS model_token_usage_global_model_idx "
            "ON model_token_usage (model_id) WHERE user_id IS NULL"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    bind.execute(sa.text("DROP TABLE IF EXISTS chat_search"))
    bind.execute(sa.text("DROP INDEX IF EXISTS chat_sidebar_default_idx"))
    bind.execute(sa.text("DROP INDEX IF EXISTS chat_message_chat_parent_idx"))
    bind.execute(sa.text("DROP INDEX IF EXISTS chat_message_chat_seq_idx"))
    bind.execute(sa.text("DROP TABLE IF EXISTS chat_message"))
