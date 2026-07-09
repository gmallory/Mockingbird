"""Synchronous database session for the Celery training worker (M9).

The rest of the gateway is async (asyncpg + SQLAlchemy ``AsyncSession``,
``app/db/session.py``), but a Celery task runs in a worker process/thread with
no event loop of its own — bridging that with ``asyncio.run()`` per task is
fragile (asyncpg's connection pool does not play well with fork + ad-hoc event
loops), so the training task gets its own plain blocking engine instead, via
the sync ``psycopg`` driver. Same DSN as the async engine, different driver.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings


def normalize_sync_dsn(url: str) -> str:
    """Coerce an asyncpg/bare Postgres DSN to the sync ``psycopg`` (v3) driver.

    Mirrors ``app.db.session.normalize_async_dsn`` for the opposite direction:
    ``.env`` ships a driver-less or ``+asyncpg`` DSN; this task needs
    ``+psycopg`` instead.
    """
    for scheme in ("postgresql+asyncpg://", "postgres://"):
        if url.startswith(scheme):
            return f"postgresql+psycopg://{url[len(scheme) :]}"
    if url.startswith("postgresql://"):
        return f"postgresql+psycopg://{url[len('postgresql://') :]}"
    return url


# Created at import, same lazy-connect posture as the async engine: building
# the engine opens no connection, so importing this module (e.g. from a
# non-worker process) touches no database.
_engine = create_engine(normalize_sync_dsn(settings.database_url), pool_pre_ping=True)
_SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)


@contextmanager
def sync_session() -> Iterator[Session]:
    """A plain blocking SQLAlchemy session, scoped to one ``with`` block."""
    session = _SessionLocal()
    try:
        yield session
    finally:
        session.close()
