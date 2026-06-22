import os

import pytest


def configure_test_database(*, required: bool = False) -> str:
    """Configure tests for the Postgres-only runtime.

    DB-backed tests must pass required=True and provide POSTGRES_TEST_DATABASE_URL
    (or an existing postgresql+asyncpg DATABASE_URL). Pure unit tests can use the
    non-required mode, which sets a dummy asyncpg URL and skips migrations so
    imports do not try to connect.
    """

    url = os.environ.get("POSTGRES_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if url and url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

    if not url or not url.startswith("postgresql+asyncpg://"):
        if required:
            pytest.skip(
                "POSTGRES_TEST_DATABASE_URL is required for this Postgres-backed test",
                allow_module_level=True,
            )
        url = "postgresql+asyncpg://test:test@127.0.0.1:5432/openwebui_test"
        os.environ.setdefault("OPEN_WEBUI_SKIP_MIGRATIONS", "true")
        os.environ.setdefault("OPEN_WEBUI_SKIP_CONFIG_DB_LOAD", "true")

    os.environ["DATABASE_URL"] = url
    os.environ.setdefault("DATABASE_POOL_SIZE", "0")
    return url
