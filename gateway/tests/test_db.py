"""Async User roundtrip against Postgres.

Proves the migration/model/session stack writes and reads a real row. Skips
cleanly when Postgres is unreachable so ``uv run pytest`` stays green without
``docker compose up`` (the real proof is the manual verification with infra up).
"""

from uuid import uuid4

import pytest
from sqlmodel import SQLModel, select

from app.db.models import Plan, User
from app.db.session import async_session, engine


async def _ensure_schema() -> None:
    """Create tables if missing; skip the test if the DB can't be reached."""
    try:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres not reachable: {exc}")


async def test_user_roundtrip() -> None:
    await _ensure_schema()
    email = f"roundtrip-{uuid4()}@example.com"

    async with async_session() as session:
        user = User(email=email, display_name="Roundtrip Tester")
        session.add(user)
        await session.commit()
        await session.refresh(user)
        user_id = user.id

    try:
        async with async_session() as session:
            fetched = (await session.execute(select(User).where(User.email == email))).scalar_one()
            assert fetched.id == user_id
            assert fetched.display_name == "Roundtrip Tester"
            assert fetched.plan is Plan.FREE
            assert fetched.monthly_minutes_used == 0.0
    finally:
        async with async_session() as session:
            stored = await session.get(User, user_id)
            if stored is not None:
                await session.delete(stored)
                await session.commit()
        # Close pooled asyncpg connections inside the running loop, otherwise
        # they are closed at GC time and emit "coroutine was never awaited".
        await engine.dispose()
