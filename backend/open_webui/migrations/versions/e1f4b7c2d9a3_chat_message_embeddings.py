"""Chat message embeddings (semantic search) — table + vchordrq ANN index

Revision ID: e1f4b7c2d9a3
Revises: d7e3f1a9c4b2
Create Date: 2026-06-20

Standalone semantic-search layer for chat search: one 4096-dim embedding per
message (qwen3-vl-embedding-8b; image messages fuse text+images). Indexed with
VectorChord's ``vchordrq`` because pgvector's own HNSW/IVFFlat cap at 2000 dims
and can't index 4096. The ANN result is RRF-fused with the lexical FTS ranking.

The vchordrq pieces are guarded on ``vchord`` being available so this migration
is a no-op-friendly on environments without VectorChord installed (the table is
still created; search degrades to lexical-only there).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "e1f4b7c2d9a3"
down_revision: Union[str, Sequence[str], None] = "d7e3f1a9c4b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("The Postgres-only runtime only supports PostgreSQL migrations")

    bind.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))

    bind.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS chat_message_embedding (
                chat_id      TEXT NOT NULL REFERENCES chat(id) ON DELETE CASCADE,
                message_id   TEXT NOT NULL,
                user_id      TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                embedding    vector(4096),
                updated_at   BIGINT NOT NULL,
                PRIMARY KEY (chat_id, message_id)
            )
            """
        )
    )
    bind.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS chat_message_embedding_user_idx "
            "ON chat_message_embedding (user_id)"
        )
    )

    # vchordrq ANN index — only if VectorChord is available on this server. Wrapped in
    # a SAVEPOINT: ``vchord`` can be "available" yet fail to CREATE if it isn't in
    # shared_preload_libraries, and that error would otherwise abort the whole
    # migration. On failure we keep the table and degrade to lexical-only search.
    has_vchord = bind.execute(
        sa.text("SELECT 1 FROM pg_available_extensions WHERE name = 'vchord'")
    ).first()
    if has_vchord:
        sp = bind.begin_nested()
        try:
            bind.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vchord CASCADE"))
            # Flat config (lists=[]) — correct for a small corpus; RaBitQ quantized scan
            # + full-precision rerank, no probes needed.
            bind.execute(
                sa.text(
                    "CREATE INDEX IF NOT EXISTS chat_message_embedding_vchordrq_idx "
                    "ON chat_message_embedding USING vchordrq (embedding vector_cosine_ops)"
                )
            )
            sp.commit()
        except Exception:
            sp.rollback()


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    bind.execute(sa.text("DROP TABLE IF EXISTS chat_message_embedding"))
