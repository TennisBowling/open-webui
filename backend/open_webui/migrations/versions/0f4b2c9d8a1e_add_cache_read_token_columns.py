"""add_cache_read_token_columns_and_events

Revision ID: 0f4b2c9d8a1e
Revises: f1a2b3c4d5e6
Create Date: 2026-05-27 00:00:00.000000

Adds cached prompt token counters and a per-request token usage event table.
Historical aggregation/backfill is intentionally kept out of the migration; run
``python -m open_webui.scripts.rebuild_token_analytics`` for a full rebuild.
"""
from typing import Sequence, Union

from alembic import op, context
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0f4b2c9d8a1e"
down_revision: Union[str, Sequence[str], None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_tables() -> set[str]:
    if context.is_offline_mode():
        return set()
    return set(sa.inspect(op.get_bind()).get_table_names())


def _existing_columns(table_name: str) -> set[str]:
    if context.is_offline_mode():
        return set()
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return set()
    return {col["name"] for col in inspector.get_columns(table_name)}


def _existing_indexes(table_name: str) -> set[str]:
    if context.is_offline_mode():
        return set()
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return set()
    return {idx["name"] for idx in inspector.get_indexes(table_name)}


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if column.name not in _existing_columns(table_name):
        op.add_column(table_name, column)


def _create_index_if_missing(name: str, table_name: str, columns: list[str]) -> None:
    if name not in _existing_indexes(table_name):
        op.create_index(name, table_name, columns)


def upgrade() -> None:
    _add_column_if_missing(
        "conversation_token_usage",
        sa.Column("total_cache_read_tokens", sa.BigInteger(), nullable=True, server_default="0"),
    )
    _add_column_if_missing(
        "conversation_token_usage",
        sa.Column("last_cache_read_tokens", sa.BigInteger(), nullable=True, server_default="0"),
    )
    _add_column_if_missing(
        "daily_token_usage",
        sa.Column("total_cache_read_tokens", sa.BigInteger(), nullable=True, server_default="0"),
    )
    _add_column_if_missing(
        "model_token_usage",
        sa.Column("total_cache_read_tokens", sa.BigInteger(), nullable=True, server_default="0"),
    )

    if "token_usage_event" not in _existing_tables():
        op.create_table(
            "token_usage_event",
            sa.Column("id", sa.String(), nullable=False, primary_key=True),
            sa.Column("user_id", sa.String(), nullable=True),
            sa.Column("source_chat_id", sa.String(), nullable=True),
            sa.Column("attributed_chat_id", sa.String(), nullable=True),
            sa.Column("message_id", sa.String(), nullable=True),
            sa.Column("parent_message_id", sa.String(), nullable=True),
            sa.Column("model_id", sa.String(), nullable=True),
            sa.Column("prompt_tokens", sa.BigInteger(), nullable=True, server_default="0"),
            sa.Column("completion_tokens", sa.BigInteger(), nullable=True, server_default="0"),
            sa.Column("total_tokens", sa.BigInteger(), nullable=True, server_default="0"),
            sa.Column("cache_read_tokens", sa.BigInteger(), nullable=True, server_default="0"),
            sa.Column("request_count", sa.Integer(), nullable=True, server_default="1"),
            sa.Column("source_type", sa.String(), nullable=True, server_default="chat"),
            sa.Column("raw_usage", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.BigInteger(), nullable=True),
        )

    _create_index_if_missing("token_usage_event_attr_chat_idx", "token_usage_event", ["attributed_chat_id"])
    _create_index_if_missing("token_usage_event_source_chat_idx", "token_usage_event", ["source_chat_id"])
    _create_index_if_missing("token_usage_event_user_ts_idx", "token_usage_event", ["user_id", "created_at"])
    _create_index_if_missing("token_usage_event_model_idx", "token_usage_event", ["model_id"])


def downgrade() -> None:
    if "token_usage_event" in _existing_tables():
        for idx_name in [
            "token_usage_event_model_idx",
            "token_usage_event_user_ts_idx",
            "token_usage_event_source_chat_idx",
            "token_usage_event_attr_chat_idx",
        ]:
            if idx_name in _existing_indexes("token_usage_event"):
                op.drop_index(idx_name, table_name="token_usage_event")
        op.drop_table("token_usage_event")

    for table_name, column_name in [
        ("model_token_usage", "total_cache_read_tokens"),
        ("daily_token_usage", "total_cache_read_tokens"),
        ("conversation_token_usage", "last_cache_read_tokens"),
        ("conversation_token_usage", "total_cache_read_tokens"),
    ]:
        if column_name in _existing_columns(table_name):
            op.drop_column(table_name, column_name)
