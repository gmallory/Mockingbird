"""Async database engine + session factory.

A single shared ``async_sessionmaker`` per the gateway spec. The engine is
lazily created so importing this module (e.g. by Alembic or tests) never opens a
connection on its own.
"""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings


def normalize_async_dsn(url: str) -> str:
    """Coerce a plain ``postgresql://`` DSN to the asyncpg driver.

    ``.env.example`` ships ``DATABASE_URL=postgresql://...`` for tooling that
    expects the bare scheme; the async engine needs the ``+asyncpg`` driver.
    """
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def create_engine() -> AsyncEngine:
    return create_async_engine(normalize_async_dsn(settings.database_url), pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


# Module-level factory for app + dependency use. Tests may build their own.
engine: AsyncEngine = create_engine()
async_session: async_sessionmaker[AsyncSession] = create_session_factory(engine)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding an ``AsyncSession``."""
    async with async_session() as session:
        yield session
