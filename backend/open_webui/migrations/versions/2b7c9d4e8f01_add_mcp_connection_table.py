"""Add user-scoped MCP connection table

Revision ID: 2b7c9d4e8f01
Revises: 0f4b2c9d8a1e
Create Date: 2026-05-31
"""

from typing import Sequence, Union

from alembic import op, context
import sqlalchemy as sa


revision: str = "2b7c9d4e8f01"
down_revision: Union[str, Sequence[str], None] = "0f4b2c9d8a1e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set() if context.is_offline_mode() else set(sa.inspect(bind).get_table_names())
    if "mcp_connection" in tables:
        return

    op.create_table(
        "mcp_connection",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("transport", sa.String(), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("command", sa.Text(), nullable=True),
        sa.Column("args", sa.Text(), nullable=True),
        sa.Column("cwd", sa.Text(), nullable=True),
        sa.Column("auth_type", sa.String(), nullable=False, server_default="none"),
        sa.Column("key", sa.Text(), nullable=True),
        sa.Column("headers", sa.Text(), nullable=True),
        sa.Column("env", sa.Text(), nullable=True),
        sa.Column("oauth", sa.Text(), nullable=True),
        sa.Column("policy", sa.Text(), nullable=True),
        sa.Column("tool_filters", sa.Text(), nullable=True),
        sa.Column("meta", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_mcp_connection_user_id", "mcp_connection", ["user_id"])
    op.create_index(
        "idx_mcp_connection_user_transport",
        "mcp_connection",
        ["user_id", "transport"],
    )


def downgrade() -> None:
    op.drop_index("idx_mcp_connection_user_transport", table_name="mcp_connection")
    op.drop_index("idx_mcp_connection_user_id", table_name="mcp_connection")
    op.drop_table("mcp_connection")
