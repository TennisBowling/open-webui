"""Add video_job table for durable video-ingest pipeline state

Revision ID: 3c9e1a7b5d24
Revises: 12dd367080c3
Create Date: 2026-07-27
"""

from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "3c9e1a7b5d24"
down_revision: Union[str, Sequence[str], None] = "12dd367080c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = (
        set() if context.is_offline_mode() else set(sa.inspect(bind).get_table_names())
    )
    if "video_job" in tables:
        return

    op.create_table(
        "video_job",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("chat_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("stage", sa.String(), nullable=False),
        sa.Column("source_type", sa.String(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("source_file_id", sa.String(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        # JSON-valued columns must be physically jsonb: the ORM binds these as
        # $N::JSONB via asyncpg, which fails outright against a text column.
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("progress", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("video_job_user_id_idx", "video_job", ["user_id"])
    # Serves the composer's rehydrate query (active jobs for one user).
    op.create_index("video_job_user_status_idx", "video_job", ["user_id", "status"])


def downgrade() -> None:
    op.drop_index("video_job_user_status_idx", table_name="video_job")
    op.drop_index("video_job_user_id_idx", table_name="video_job")
    op.drop_table("video_job")
