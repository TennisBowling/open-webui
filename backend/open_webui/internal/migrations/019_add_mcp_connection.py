"""Peewee migration: add user-scoped MCP connection table."""

from contextlib import suppress

import peewee as pw
from peewee_migrate import Migrator


with suppress(ImportError):
    import playhouse.postgres_ext as pw_pext


def migrate(migrator: Migrator, database: pw.Database, *, fake=False):
    @migrator.create_model
    class MCPConnection(pw.Model):
        id = pw.TextField(unique=True)
        user_id = pw.TextField(index=True)
        name = pw.TextField()
        description = pw.TextField(null=True)
        transport = pw.TextField()
        url = pw.TextField(null=True)
        command = pw.TextField(null=True)
        args = pw.TextField(null=True)
        cwd = pw.TextField(null=True)
        auth_type = pw.TextField(default="none")
        key = pw.TextField(null=True)
        headers = pw.TextField(null=True)
        env = pw.TextField(null=True)
        oauth = pw.TextField(null=True)
        policy = pw.TextField(null=True)
        tool_filters = pw.TextField(null=True)
        meta = pw.TextField(null=True)
        enabled = pw.BooleanField(default=True)
        updated_at = pw.BigIntegerField(null=False)
        created_at = pw.BigIntegerField(null=False)

        class Meta:
            table_name = "mcp_connection"


def rollback(migrator: Migrator, database: pw.Database, *, fake=False):
    migrator.remove_model("mcp_connection")
