from alembic import op
from alembic import context
from sqlalchemy import inspect


def get_existing_tables():
    if context.is_offline_mode():
        return set()
    con = op.get_bind()
    return set(inspect(con).get_table_names())


def get_revision_id():
    import uuid

    return str(uuid.uuid4()).replace("-", "")[:12]
