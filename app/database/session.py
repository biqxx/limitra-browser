from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings
from app.database.models import Base

logger = structlog.get_logger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None



def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        is_sqlite = settings.DATABASE_URL.startswith("sqlite")
        engine_kwargs: dict[str, Any] = {"echo": settings.DEBUG}
        if not is_sqlite:
            engine_kwargs.update(
                {
                    "pool_size": settings.DATABASE_POOL_SIZE,
                    "max_overflow": settings.DATABASE_MAX_OVERFLOW,
                    "pool_timeout": settings.DATABASE_POOL_TIMEOUT,
                }
            )
        _engine = create_async_engine(settings.DATABASE_URL, **engine_kwargs)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autocommit=False,
            autoflush=False,
        )
    return _sessionmaker


def reset_db_engine() -> None:
    """Reset cached engine and sessionmaker so changes to DATABASE_URL take effect."""
    global _engine, _sessionmaker
    _engine = None
    _sessionmaker = None


# Module-level references for direct imports
class _EngineProxy:
    def __getattr__(self, name: str) -> Any:
        return getattr(get_engine(), name)

    def begin(self) -> Any:
        return get_engine().begin()


class _SessionmakerProxy:
    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return get_sessionmaker()(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(get_sessionmaker(), name)


engine = _EngineProxy()
AsyncSessionLocal = _SessionmakerProxy()


@asynccontextmanager
async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    async with get_sessionmaker()() as session:
        try:
            yield session
            if session.is_active:
                await session.commit()
        except Exception:
            if session.is_active:
                await session.rollback()
            raise


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with get_db_session() as session:
        yield session


async def init_db() -> None:
    settings = get_settings()
    # In production, tables must be managed exclusively via Alembic migrations.
    if settings.ENVIRONMENT.lower() == "production":
        return

    eng = get_engine()
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def get_expected_alembic_head() -> str | None:
    """Programmatically determines the current Alembic head revision."""
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config("alembic.ini")
        script = ScriptDirectory.from_config(cfg)
        return script.get_current_head()
    except Exception as e:
        logger.warning("failed_to_resolve_alembic_head", error=str(e))
        return None


async def check_alembic_head_revision(
    expected_revision: str | None = None,
) -> tuple[bool, str | None]:
    """
    Checks if the database revision in `alembic_version` table matches the expected head.
    Determines expected revision programmatically from Alembic scripts if not provided.
    Returns (is_current, current_revision).
    """
    from sqlalchemy import text

    if expected_revision is None:
        expected_revision = get_expected_alembic_head()

    eng = get_engine()
    try:
        async with eng.connect() as conn:
            res = await conn.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))
            row = res.scalar_one_or_none()
            if expected_revision and row == expected_revision:
                return True, row
            return False, row
    except Exception as e:
        return False, str(e)

