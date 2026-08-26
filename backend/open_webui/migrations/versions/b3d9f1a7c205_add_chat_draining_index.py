"""Add partial index on chat draining marker for the queue-drain sweeper

Revision ID: b3d9f1a7c205
Revises: c5e2a7f1b94d
Create Date: 2026-06-22

The autonomous message-queue drain sweeper enumerates chats that are mid-drain
with ``SELECT id FROM chat WHERE jsonb_typeof(chat->'draining') = 'object'`` on a
short interval (utils/chat_queue.py ``get_chat_ids_with_draining_marker``).
Without a supporting index this is a full sequential scan that has to detoast
every chat's large JSONB ``chat`` blob just to read ``chat->'draining'``
(~95ms over ~4.5k rows on the dev instance; cost grows linearly with the table).

A PARTIAL expression index over only the rows whose ``draining`` is an object —
the handful actually mid-drain at any instant, usually zero — makes that lookup a
tiny index(-only) scan. The partial WHERE matches the sweeper query's predicate
exactly, so the planner can use it; the index stays near-empty because completed
generations clear their own marker.

Postgres-only DDL, matching the Postgres-only runtime. Idempotent (IF NOT EXISTS)
so re-applying / a pre-created index is harmless.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import open_webui.internal.db

# revision identifiers, used by Alembic.
revision: str = "b3d9f1a7c205"
down_revision: Union[str, None] = "c5e2a7f1b94d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX_NAME = "chat_draining_object_idx"


def upgrade() -> None:
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {_INDEX_NAME} "
        "ON chat (id) "
        "WHERE jsonb_typeof(chat->'draining') = 'object'"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_INDEX_NAME}")
