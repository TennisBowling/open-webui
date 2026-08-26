"""Fix mcp_connection JSON columns: text -> jsonb

The original mcp_connection migration (2b7c9d4e8f01) created args/policy/
tool_filters/meta as ``text``, but the ORM maps them to ``JSONField`` whose
``impl`` is ``JSONB``. On the Postgres+asyncpg runtime the bind renders
``$N::JSONB``; assigning a jsonb expression into a ``text`` column raises
(no implicit jsonb->text cast), and the error is swallowed by the model layer,
so EVERY per-user MCP connection write silently failed.

2b7c9d4e8f01 has been corrected for fresh installs. This migration repairs
databases that already ran the old (text) version by altering the four columns
to ``jsonb``. It is idempotent: columns already jsonb are skipped, so a fresh
install (where 2b7c9d4e8f01 now creates jsonb) is a no-op here.

Revision ID: a1b2c3d4e5f6
Revises: b3d9f1a7c205
Create Date: 2026-06-24
"""

from typing import Sequence, Union

from alembic import op, context
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "b3d9f1a7c205"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_JSON_COLUMNS = ("args", "policy", "tool_filters", "meta")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql" or context.is_offline_mode():
        return
    insp = sa.inspect(bind)
    if "mcp_connection" not in insp.get_table_names():
        return
    existing = {c["name"]: c for c in insp.get_columns("mcp_connection")}
    for name in _JSON_COLUMNS:
        col = existing.get(name)
        if not col:
            continue
        if "json" in str(col["type"]).lower():
            # already json/jsonb (fresh install, or re-run) — leave it.
            continue
        op.alter_column(
            "mcp_connection",
            name,
            existing_type=sa.Text(),
            type_=postgresql.JSONB(),
            existing_nullable=True,
            # text rows can only be NULL or valid JSON; cast explicitly.
            postgresql_using=f"{name}::jsonb",
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql" or context.is_offline_mode():
        return
    insp = sa.inspect(bind)
    if "mcp_connection" not in insp.get_table_names():
        return
    existing = {c["name"]: c for c in insp.get_columns("mcp_connection")}
    for name in _JSON_COLUMNS:
        col = existing.get(name)
        if not col:
            continue
        if "json" not in str(col["type"]).lower():
            continue
        op.alter_column(
            "mcp_connection",
            name,
            existing_type=postgresql.JSONB(),
            type_=sa.Text(),
            existing_nullable=True,
            postgresql_using=f"{name}::text",
        )
