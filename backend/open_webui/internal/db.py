import json
import logging
from contextvars import ContextVar
from typing import Any, AsyncIterator, Optional

import orjson
from sqlalchemy import Dialect, MetaData, types
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import declarative_base
from sqlalchemy.pool import NullPool
from sqlalchemy.sql.type_api import _T
from typing_extensions import Self

from open_webui.env import (
    DATABASE_POOL_MAX_OVERFLOW,
    DATABASE_POOL_RECYCLE,
    DATABASE_POOL_SIZE,
    DATABASE_POOL_TIMEOUT,
    DATABASE_SCHEMA,
    DATABASE_URL,
    SRC_LOG_LEVELS,
)

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["DB"])

_sync_session_ctx: ContextVar[Any | None] = ContextVar("sync_session_ctx", default=None)


class JSONField(types.TypeDecorator):
    """Legacy model JSON field stored as native PostgreSQL JSONB.

    Older SQLite deployments stored these values as JSON-encoded TEXT. The
    Postgres-only runtime stores Python values directly as JSONB while keeping
    the small Peewee-style helpers some model code still calls.
    """

    impl = JSONB
    cache_ok = True

    def process_bind_param(self, value: Optional[_T], dialect: Dialect) -> Any:
        return value

    def process_result_value(self, value: Optional[_T], dialect: Dialect) -> Any:
        return value

    def copy(self, **kw: Any) -> Self:
        return JSONField()

    def db_value(self, value):
        return value

    def python_value(self, value):
        if isinstance(value, str):
            try:
                return json.loads(value)
            except Exception:
                return value
        return value


class OrjsonJSON(types.TypeDecorator):
    """JSONB field with fast parsing fallback for legacy string rows."""

    impl = JSONB
    cache_ok = True

    def process_bind_param(self, value: Optional[_T], dialect: Dialect) -> Any:
        return value

    def process_result_value(self, value: Optional[_T], dialect: Dialect) -> Any:
        if isinstance(value, str):
            return orjson.loads(value)
        return value

    def copy(self, **kw: Any) -> Self:
        return OrjsonJSON()


SQLALCHEMY_DATABASE_URL = DATABASE_URL

_engine_kwargs: dict[str, Any] = {
    "pool_pre_ping": True,
}

if isinstance(DATABASE_POOL_SIZE, int):
    if DATABASE_POOL_SIZE > 0:
        _engine_kwargs.update(
            {
                "pool_size": DATABASE_POOL_SIZE,
                "max_overflow": DATABASE_POOL_MAX_OVERFLOW,
                "pool_timeout": DATABASE_POOL_TIMEOUT,
                "pool_recycle": DATABASE_POOL_RECYCLE,
            }
        )
    else:
        _engine_kwargs["poolclass"] = NullPool

engine: AsyncEngine = create_async_engine(SQLALCHEMY_DATABASE_URL, **_engine_kwargs)

SessionLocal = async_sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
)

metadata_obj = MetaData(schema=DATABASE_SCHEMA)
Base = declarative_base(metadata=metadata_obj)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as db:
        yield db


class _DBContext:
    def __init__(self):
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> AsyncSession:
        self._session = SessionLocal()
        return self._session

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session is not None:
            await self._session.close()

    def __enter__(self):
        sync_session = _sync_session_ctx.get()
        if sync_session is None:
            raise RuntimeError("Synchronous get_db() is only available inside run_sync_db")
        return sync_session

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def get_db() -> _DBContext:
    return _DBContext()


async def run_sync_db(fn):
    async with SessionLocal() as db:
        def _run(sync_session):
            token = _sync_session_ctx.set(sync_session)
            try:
                return fn()
            finally:
                _sync_session_ctx.reset(token)

        return await db.run_sync(_run)


async def dispose_engine() -> None:
    await engine.dispose()
