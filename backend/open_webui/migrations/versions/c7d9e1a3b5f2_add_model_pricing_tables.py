"""add_model_pricing_tables_and_embedded_cost

Revision ID: c7d9e1a3b5f2
Revises: 5d4a8f9c2b71
Create Date: 2026-06-19 00:00:00.000000

Adds USD cost calculation support:
- ``model_pricing_catalog``: per-model rates synced from OpenRouter.
- ``model_pricing_override``: admin-managed alias/manual/zero mappings keyed by
  the exact stored ``token_usage_event.model_id``.
- ``token_usage_event.embedded_cost``: authoritative per-call USD cost for
  OpenRouter-routed rows, precomputed once so the read path never parses JSON.

The embedded-cost backfill (``cost`` else ``upstream_inference_cost``) is run
once here and is idempotent via the ``embedded_cost IS NULL`` guard.
"""
from typing import Sequence, Union

from alembic import op, context
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c7d9e1a3b5f2"
down_revision: Union[str, Sequence[str], None] = "5d4a8f9c2b71"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_tables() -> set:
    if context.is_offline_mode():
        return set()
    return set(sa.inspect(op.get_bind()).get_table_names())


def _existing_columns(table_name: str) -> set:
    if context.is_offline_mode():
        return set()
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return set()
    return {col["name"] for col in inspector.get_columns(table_name)}


def _existing_indexes(table_name: str) -> set:
    if context.is_offline_mode():
        return set()
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return set()
    return {idx["name"] for idx in inspector.get_indexes(table_name)}


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if table_name in _existing_tables() and column.name not in _existing_columns(table_name):
        op.add_column(table_name, column)


def _create_index_if_missing(name: str, table_name: str, columns: list) -> None:
    if name not in _existing_indexes(table_name):
        op.create_index(name, table_name, columns)


def upgrade() -> None:
    # --- model_pricing_catalog (synced from OpenRouter) ---
    if "model_pricing_catalog" not in _existing_tables():
        op.create_table(
            "model_pricing_catalog",
            sa.Column("slug", sa.String(), nullable=False, primary_key=True),
            sa.Column("model_name", sa.String(), nullable=True),
            sa.Column("prompt_rate", sa.Float(), nullable=True),
            sa.Column("completion_rate", sa.Float(), nullable=True),
            sa.Column("cache_read_rate", sa.Float(), nullable=True),
            sa.Column("web_search_rate", sa.Float(), nullable=True),
            sa.Column("is_free", sa.Boolean(), nullable=True, server_default=sa.false()),
            sa.Column("raw_pricing", sa.JSON(), nullable=True),
            sa.Column("synced_at", sa.BigInteger(), nullable=True),
        )

    # --- model_pricing_override (admin-managed) ---
    if "model_pricing_override" not in _existing_tables():
        op.create_table(
            "model_pricing_override",
            sa.Column("model_id", sa.String(), nullable=False, primary_key=True),
            sa.Column("mode", sa.String(), nullable=False, server_default="alias"),
            sa.Column("alias_slug", sa.String(), nullable=True),
            sa.Column("prompt_rate", sa.Float(), nullable=True),
            sa.Column("completion_rate", sa.Float(), nullable=True),
            sa.Column("cache_read_rate", sa.Float(), nullable=True),
            sa.Column("note", sa.String(), nullable=True),
            sa.Column("updated_by", sa.String(), nullable=True),
            sa.Column("created_at", sa.BigInteger(), nullable=True),
            sa.Column("updated_at", sa.BigInteger(), nullable=True),
        )

    # --- token_usage_event.embedded_cost (authoritative, precomputed) ---
    _add_column_if_missing(
        "token_usage_event",
        sa.Column("embedded_cost", sa.Float(), nullable=True),
    )

    # One-time idempotent backfill of embedded cost for OpenRouter-routed rows.
    # cost if cost != 0 else cost_details.upstream_inference_cost (BYOK fallback).
    # Guarded by embedded_cost IS NULL so re-runs on every boot are no-ops.
    # Skipped in offline mode and only when the JSON column supports the ? operator
    # (Postgres jsonb). On non-Postgres the column simply stays NULL.
    if not context.is_offline_mode():
        bind = op.get_bind()
        if bind.dialect.name == "postgresql":
            op.execute(
                sa.text(
                    """
                    UPDATE token_usage_event
                    SET embedded_cost = CASE
                        WHEN COALESCE((raw_usage->>'cost')::float8, 0) <> 0
                            THEN (raw_usage->>'cost')::float8
                        ELSE COALESCE((raw_usage->'cost_details'->>'upstream_inference_cost')::float8, 0)
                    END
                    WHERE raw_usage ? 'cost' AND embedded_cost IS NULL
                    """
                )
            )


def downgrade() -> None:
    if "embedded_cost" in _existing_columns("token_usage_event"):
        op.drop_column("token_usage_event", "embedded_cost")

    if "model_pricing_override" in _existing_tables():
        op.drop_table("model_pricing_override")

    if "model_pricing_catalog" in _existing_tables():
        op.drop_table("model_pricing_catalog")
