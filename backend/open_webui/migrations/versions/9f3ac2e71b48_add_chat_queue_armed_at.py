"""Add chat.queue_armed_at (indexed) so pending queues are cheaply enumerable

Revision ID: 9f3ac2e71b48
Revises: 3c9e1a7b5d24
Create Date: 2026-07-29

The server-driven message queue was drained by exactly ONE event: the tail of a
clean generation. Every way that event can be missed — a worker restart mid-turn,
a drain that bailed because the per-chat lock or the turn lease was momentarily
held, a completion that took an error path — left the queue sitting in
``chat.chat["queue"]`` with nothing that would ever look at it again. The only
backstop, ``sweep_orphaned_drains``, is keyed on the ``draining`` MARKER, so it
sees a drain that died mid-flight but is blind to a queue that was never drained
in the first place.

The fix is a reconciler over "chats that are owed a drain", which needs those
chats to be enumerable without scanning the table: on this instance a
``jsonb_array_length(chat->'queue') > 0`` scan reads ~6.5k buffers / 131ms
(6.4k chats, 86MB of TOAST), which is not something to repeat every 15 seconds.

A COLUMN rather than another key in the ``chat`` blob because the blob is
whole-column rewritten by routine autosaves; ``update_chat_by_id`` has to
re-inject live ``queue`` / ``draining`` / ``question_states`` by hand to stop
stale client snapshots clobbering them, and a fourth key to remember is a
drift hazard. A column is structurally immune and indexes as a plain scalar.

NOT backfilled, deliberately. 564 chats on this instance already carry a
non-empty queue — nearly all of them long-dead test fixtures, the rest abandoned
a month or more ago. Arming them retroactively would fire hundreds of real
generations on the next restart. The flag means "the server has accepted
responsibility for this queue", which it can only truthfully say for queue
writes it performs from now on.
"""

import sqlalchemy as sa
from alembic import op

revision = "9f3ac2e71b48"
down_revision = "3c9e1a7b5d24"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    dialect = bind.dialect.name

    columns = {c["name"] for c in sa.inspect(bind).get_columns("chat")}
    if "queue_armed_at" not in columns:
        op.add_column(
            "chat", sa.Column("queue_armed_at", sa.BigInteger(), nullable=True)
        )

    indexes = {i["name"] for i in sa.inspect(bind).get_indexes("chat")}
    if "chat_queue_armed_idx" not in indexes:
        # Partial: the index only ever holds the handful of chats with work
        # outstanding, so the reconciler's lookup is a couple of buffer hits
        # regardless of how large the chat table grows. SQLite understands the
        # same partial-index syntax; other dialects get a plain index.
        kwargs = {}
        if dialect == "postgresql":
            kwargs["postgresql_where"] = sa.text("queue_armed_at IS NOT NULL")
        elif dialect == "sqlite":
            kwargs["sqlite_where"] = sa.text("queue_armed_at IS NOT NULL")
        op.create_index("chat_queue_armed_idx", "chat", ["queue_armed_at"], **kwargs)


def downgrade():
    bind = op.get_bind()
    indexes = {i["name"] for i in sa.inspect(bind).get_indexes("chat")}
    if "chat_queue_armed_idx" in indexes:
        op.drop_index("chat_queue_armed_idx", table_name="chat")
    columns = {c["name"] for c in sa.inspect(bind).get_columns("chat")}
    if "queue_armed_at" in columns:
        op.drop_column("chat", "queue_armed_at")
