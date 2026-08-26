"""Regression guard for the mcp_connection JSON-column schema (audit C1).

The ORM maps args/policy/tool_filters/meta to JSONField (impl=JSONB). On the
Postgres+asyncpg runtime that binds ``$N::JSONB``; if the physical column is
``text`` the write raises and is swallowed, so EVERY per-user MCP connection
create silently fails. These tests pin model-side and migration-side types to
JSONB so the mismatch cannot return, and confirm the encrypted secret columns
stay ``text``.
"""

import os
import re

import open_webui
from open_webui.internal.db import JSONField
from open_webui.models.mcp import MCPConnection
from sqlalchemy import Text


JSON_COLUMNS = ("args", "policy", "tool_filters", "meta")
SECRET_COLUMNS = ("key", "headers", "env", "oauth")

_MIGRATION = os.path.join(
    os.path.dirname(open_webui.__file__),
    "migrations",
    "versions",
    "2b7c9d4e8f01_add_mcp_connection_table.py",
)


def test_model_json_columns_are_jsonb():
    cols = MCPConnection.__table__.c
    for name in JSON_COLUMNS:
        assert isinstance(
            cols[name].type, JSONField
        ), f"mcp_connection.{name} must map to JSONField(JSONB), got {cols[name].type!r}"


def test_model_secret_columns_are_text():
    # Secret columns are Fernet-encrypted to a string before persistence; they
    # must stay Text (binding a dict as JSONB here would re-introduce a mismatch).
    cols = MCPConnection.__table__.c
    for name in SECRET_COLUMNS:
        assert isinstance(
            cols[name].type, Text
        ), f"mcp_connection.{name} must be Text, got {cols[name].type!r}"


def test_initial_migration_declares_jsonb_columns():
    src = open(_MIGRATION, encoding="utf-8").read()
    for name in JSON_COLUMNS:
        # The column must be created as JSONB, never sa.Text(), or Postgres
        # writes break (audit C1).
        assert re.search(
            rf'sa\.Column\(\s*"{name}",\s*postgresql\.JSONB\(\)', src
        ), f"migration must create mcp_connection.{name} as postgresql.JSONB()"
        assert not re.search(
            rf'sa\.Column\(\s*"{name}",\s*sa\.Text\(\)', src
        ), f"migration must not create mcp_connection.{name} as sa.Text()"


def test_model_insert_binds_json_columns_as_jsonb():
    """The asyncpg bind must render ::JSONB for exactly the JSON columns."""
    from sqlalchemy import insert
    from sqlalchemy.dialects.postgresql import asyncpg

    stmt = insert(MCPConnection).values(
        id="x",
        user_id="u",
        name="n",
        transport="remote_http",
        args=["a"],
        policy={"enable_write_tools": True},
        tool_filters={"include": ["search"]},
        meta={"template": "x"},
        enabled=True,
        updated_at=0,
        created_at=0,
    )
    sql = str(stmt.compile(dialect=asyncpg.dialect()))
    # One ::JSONB per JSON column we set; none of the text columns should cast.
    assert sql.count("::JSONB") == len(JSON_COLUMNS), sql
