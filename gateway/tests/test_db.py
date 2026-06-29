"""Async User roundtrip against Postgres.

Exercises the model + async-session stack: writes and reads a real row. Schema
is set up here via ``SQLModel.metadata.create_all`` (not Alembic); the migration
itself is verified separately by ``alembic upgrade head``. Skips cleanly when
Postgres is unreachable so ``uv run pytest`` stays green without
``docker compose up`` (the real proof is the manual verification with infra up).
"""

from uuid import uuid4

import pytest
from sqlmodel import SQLModel, select

from app.db.models import Plan, User
from app.db.session import create_engine, create_session_factory


async def _ensure_schema(engine) -> None:
    """Create tables if missing; skip the test if the DB can't be reached."""
    try:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres not reachable: {exc}")


async def test_user_roundtrip() -> None:
    # Own engine bound to this test's event loop, not the module-level shared
    # one. pytest-asyncio runs each test in a fresh loop, so reusing a single
    # engine across async tests would attach its asyncpg pool to a dead loop.
    engine = create_engine()
    async_session = create_session_factory(engine)
    email = f"roundtrip-{uuid4()}@example.com"
    user_id = None

    # Whole body (including the initial write) is under try/finally so a failure
    # anywhere still disposes the pool inside the running loop — otherwise the
    # connections close at GC time and emit "coroutine was never awaited".
    try:
        await _ensure_schema(engine)

        async with async_session() as session:
            user = User(email=email, display_name="Roundtrip Tester")
            session.add(user)
            await session.commit()
            await session.refresh(user)
            user_id = user.id

        async with async_session() as session:
            fetched = (await session.execute(select(User).where(User.email == email))).scalar_one()
            assert fetched.id == user_id
            assert fetched.display_name == "Roundtrip Tester"
            assert fetched.plan is Plan.FREE
            assert fetched.monthly_minutes_used == 0.0
    finally:
        try:
            # Delete in finally so the row is cleaned up even on assertion failure.
            if user_id is not None:
                async with async_session() as session:
                    stored = await session.get(User, user_id)
                    if stored is not None:
                        await session.delete(stored)
                        await session.commit()
        finally:
            await engine.dispose()
