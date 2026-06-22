"""Search index cleanup

Revision ID: d7e3f1a9c4b2
Revises: c7d9e1a3b5f2
Create Date: 2026-06-19

Postgres-only cleanup for the chat search tables:
- Drops the dead, write-only `chat.search_text` column.
- Strips inline base64 data-URLs from the existing FTS rows so they stop
  polluting the search index. The GENERATED search_vector columns update
  automatically when `content` / `body` change.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "d7e3f1a9c4b2"
down_revision: Union[str, Sequence[str], None] = "c7d9e1a3b5f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Matches inline data-URLs like `data:image/png;base64,iVBOR...` and replaces
# the whole token with `[image]` so the FTS index stays clean.
_DATA_URL_REGEX = "data:image/[a-zA-Z0-9.+/_-]*;base64,[A-Za-z0-9+/=]+"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("The Postgres-only runtime only supports PostgreSQL migrations")

    # a. Drop the dead write-only column.
    bind.execute(sa.text("ALTER TABLE chat DROP COLUMN IF EXISTS search_text"))

    # b. Strip inline base64 data-URLs from existing search rows (cheap, ~84 rows).
    #    The GENERATED search_vector columns update automatically.
    bind.execute(
        sa.text(
            "UPDATE chat_message_search "
            "SET content = regexp_replace(content, :pattern, '[image]', 'g') "
            "WHERE content LIKE '%base64,%'"
        ),
        {"pattern": _DATA_URL_REGEX},
    )
    bind.execute(
        sa.text(
            "UPDATE chat_search "
            "SET body = regexp_replace(body, :pattern, '[image]', 'g') "
            "WHERE body LIKE '%base64,%'"
        ),
        {"pattern": _DATA_URL_REGEX},
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # Re-add the dropped column. The base64 content strip is NOT reversible
    # (the original data-URLs were discarded), so it is intentionally left as-is.
    bind.execute(sa.text("ALTER TABLE chat ADD COLUMN IF NOT EXISTS search_text TEXT"))
