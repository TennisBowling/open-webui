"""Add unique partial index on file (user_id, meta->data->upload_id)

The upload-idempotency lookup (Files.get_file_by_user_id_and_upload_id,
used by routers/files.py's upload handler) checks for an existing row with
the same (user_id, upload_id) before inserting a new one. That SELECT-then-
INSERT is not atomic: two near-simultaneous requests carrying the same
upload_id (exactly the retry scenario this feature targets) can both pass
the SELECT before either commits its INSERT, producing two rows with the
same upload_id.

This migration adds a unique index over (user_id, meta->'data'->>'upload_id')
for rows where that path is non-null, so a concurrent duplicate insert now
fails with an IntegrityError instead of silently succeeding. The upload
handler catches that error and re-reads the winning row (see
FilesTable.insert_new_file in models/files.py).

It's a partial + expression index (JSONB path + WHERE clause), which the
declarative op.create_index() helper doesn't support, so it's created via
raw SQL like other Postgres-specific indexes in this directory
(see b1f1e9a4c7d2_file_user_id_index.py).

This also makes the existing dedup SELECT (previously an unindexed JSONB
path scan over all of a user's files) fast.

Revision ID: 12dd367080c3
Revises: a1b2c3d4e5f6
Create Date: 2026-07-09
"""

from typing import Sequence, Union

from alembic import op, context
import sqlalchemy as sa

revision: str = "12dd367080c3"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_INDEX_NAME = "file_user_id_upload_id_unique_idx"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql" or context.is_offline_mode():
        # The upload_id dedup path is only exercised on Postgres deployments;
        # sqlite/test setups don't need the constraint.
        return
    insp = sa.inspect(bind)
    if "file" not in insp.get_table_names():
        return
    bind.execute(
        sa.text(
            f'CREATE UNIQUE INDEX IF NOT EXISTS {_INDEX_NAME} '
            f'ON "file" (user_id, (meta -> \'data\' ->> \'upload_id\')) '
            f"WHERE meta -> 'data' ->> 'upload_id' IS NOT NULL"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql" or context.is_offline_mode():
        return
    bind.execute(sa.text(f"DROP INDEX IF EXISTS {_INDEX_NAME}"))
