"""Add automation / automation_run / push_subscription tables + chat.automation_of

Revision ID: 4c1e7a6b9d30
Revises: 9f3ac2e71b48
Create Date: 2026-07-31
"""

from typing import Sequence, Union

from alembic import op, context
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "4c1e7a6b9d30"
down_revision: Union[str, Sequence[str], None] = "9f3ac2e71b48"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set() if context.is_offline_mode() else set(sa.inspect(bind).get_table_names())

    if "automation" not in tables:
        op.create_table(
            "automation",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column("prompt", sa.Text(), nullable=False),
            sa.Column("rrule", sa.Text(), nullable=True),
            sa.Column("dtstart", sa.BigInteger(), nullable=False),
            sa.Column("timezone", sa.String(), nullable=False),
            sa.Column("model_id", sa.String(), nullable=False),
            # JSON-valued columns: the ORM maps these to JSONField (impl=JSONB),
            # so the physical type MUST be jsonb — declaring them sa.Text() makes
            # the asyncpg bind ($N::JSONB) fail against a text column.
            sa.Column("tool_ids", postgresql.JSONB(), nullable=True),
            sa.Column("features", postgresql.JSONB(), nullable=True),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("next_run_at", sa.BigInteger(), nullable=True),
            sa.Column("last_run_at", sa.BigInteger(), nullable=True),
            sa.Column("last_run_status", sa.String(), nullable=True),
            sa.Column("updated_at", sa.BigInteger(), nullable=False),
            sa.Column("created_at", sa.BigInteger(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("automation_user_id_idx", "automation", ["user_id"])
        op.create_index(
            "automation_due_idx",
            "automation",
            ["next_run_at"],
            postgresql_where=sa.text("active AND next_run_at IS NOT NULL"),
        )

    if "automation_run" not in tables:
        op.create_table(
            "automation_run",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("automation_id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("chat_id", sa.String(), nullable=True),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("preview", sa.Text(), nullable=True),
            sa.Column("seen", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("started_at", sa.BigInteger(), nullable=False),
            sa.Column("ended_at", sa.BigInteger(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "automation_run_automation_id_idx", "automation_run", ["automation_id"]
        )
        op.create_index(
            "automation_run_unseen_idx",
            "automation_run",
            ["user_id"],
            postgresql_where=sa.text("seen = false"),
        )

    if "push_subscription" not in tables:
        op.create_table(
            "push_subscription",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("endpoint", sa.Text(), nullable=False),
            sa.Column("keys", postgresql.JSONB(), nullable=False),
            sa.Column("user_agent", sa.Text(), nullable=True),
            sa.Column("created_at", sa.BigInteger(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("endpoint"),
        )
        op.create_index("push_subscription_user_id_idx", "push_subscription", ["user_id"])

    chat_columns = (
        set()
        if context.is_offline_mode()
        else {c["name"] for c in sa.inspect(bind).get_columns("chat")}
    )
    if "automation_of" not in chat_columns:
        op.add_column("chat", sa.Column("automation_of", sa.String(), nullable=True))
    op.create_index(
        "chat_automation_of_idx",
        "chat",
        ["automation_of"],
        postgresql_where=sa.text("automation_of IS NOT NULL"),
        if_not_exists=True,
    )

    # The sidebar's default-view index carries the hidden-chat predicate, so it
    # has to learn about automation runs too — otherwise the new
    # `automation_of IS NULL` clause in the sidebar query can't use it and the
    # default chat list falls off its index-only scan.
    op.drop_index("chat_sidebar_default_idx", table_name="chat", if_exists=True)
    op.create_index(
        "chat_sidebar_default_idx",
        "chat",
        ["user_id", sa.text("updated_at DESC")],
        postgresql_where=sa.text(
            "archived = false AND folder_id IS NULL "
            "AND (pinned = false OR pinned IS NULL) AND subagent_of IS NULL "
            "AND automation_of IS NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("chat_sidebar_default_idx", table_name="chat", if_exists=True)
    op.create_index(
        "chat_sidebar_default_idx",
        "chat",
        ["user_id", sa.text("updated_at DESC")],
        postgresql_where=sa.text(
            "archived = false AND folder_id IS NULL "
            "AND (pinned = false OR pinned IS NULL) AND subagent_of IS NULL"
        ),
    )
    op.drop_index("chat_automation_of_idx", table_name="chat", if_exists=True)
    op.drop_column("chat", "automation_of")

    op.drop_index("push_subscription_user_id_idx", table_name="push_subscription")
    op.drop_table("push_subscription")

    op.drop_index("automation_run_unseen_idx", table_name="automation_run")
    op.drop_index("automation_run_automation_id_idx", table_name="automation_run")
    op.drop_table("automation_run")

    op.drop_index("automation_due_idx", table_name="automation")
    op.drop_index("automation_user_id_idx", table_name="automation")
    op.drop_table("automation")
