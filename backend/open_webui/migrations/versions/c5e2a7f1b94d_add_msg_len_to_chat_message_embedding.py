"""Add msg_len to chat_message_embedding (semantic-search content-length gate)

Revision ID: c5e2a7f1b94d
Revises: e1f4b7c2d9a3
Create Date: 2026-06-22

The semantic-search arm skips contentless message fragments ("whatever", "which one",
raw scaffolding) — they form a degenerate embedding centroid that sits closer to typo /
out-of-domain queries than any real topic and floods results with confident junk. Filtering
on stripped content length at query time would need a per-row regex+join (measured ~15-25x
slower), so the length is precomputed here into ``msg_len`` and filtered as a plain column
(``_SEARCH_SEM_MIN_MSG_LEN`` in models/chats.py). Image messages get a high sentinel so the
filter never drops them. The sweep (scripts/backfill_chat_embeddings.py) maintains it on write.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c5e2a7f1b94d"
down_revision: Union[str, Sequence[str], None] = "e1f4b7c2d9a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("The Postgres-only runtime only supports PostgreSQL migrations")

    bind.execute(sa.text("ALTER TABLE chat_message_embedding ADD COLUMN IF NOT EXISTS msg_len INT"))
    # Backfill stripped content length (image messages -> sentinel 10000 so they're never
    # filtered out). Idempotent: only fills rows not yet set. One pass, ~seconds at this scale.
    bind.execute(
        sa.text(
            """
            UPDATE chat_message_embedding e SET msg_len = (
                CASE WHEN EXISTS (
                        SELECT 1 FROM jsonb_array_elements(coalesce(cm.meta->'files','[]'::jsonb)) f
                        WHERE f->>'type' = 'image')
                     THEN 10000
                     ELSE length(trim(
                            CASE WHEN cm.role = 'assistant'
                                 THEN regexp_replace(cm.content, '<details.*?</details>', '', 'g')
                                 ELSE cm.content END))
                END)
            FROM chat_message cm
            WHERE cm.chat_id = e.chat_id AND cm.message_id = e.message_id AND e.msg_len IS NULL
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    bind.execute(sa.text("ALTER TABLE chat_message_embedding DROP COLUMN IF EXISTS msg_len"))
