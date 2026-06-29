"""Async database engine + session factory.

A single shared ``async_sessionmaker`` per the gateway spec. The engine is
instantiated at import, but creating it opens no connection: asyncpg connects
lazily on first use, so importing this module (e.g. by Alembic or tests) does
not touch the database.
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
    """Coerce a bare Postgres DSN to the asyncpg driver.

    ``.env.example`` ships ``DATABASE_URL=postgresql://...`` for tooling that
    expects the bare scheme; the async engine needs the ``+asyncpg`` driver.
    Both ``postgresql://`` and the ``postgres://`` alias (libpq/Heroku-style) are
    handled; a DSN that already names a driver is left untouched.
    """
    for scheme in ("postgresql://", "postgres://"):
        if url.startswith(scheme):
            return f"postgresql+asyncpg://{url[len(scheme) :]}"
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
