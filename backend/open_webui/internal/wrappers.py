"""Legacy Peewee compatibility shim.

The Postgres-only async runtime no longer runs Peewee migrations or opens
Peewee connections. This module remains only so stale external imports fail
with a clear error instead of importing optional dependencies that are no longer
installed.
"""


def register_connection(db_url):
    raise RuntimeError(
        "Peewee database wrappers were removed in the Postgres-only async runtime. "
        "Use open_webui.internal.db and Alembic migrations instead."
    )
