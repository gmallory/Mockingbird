"""Gateway database models (SQLModel).

Slim M2 ships only ``User`` — the FK root that the rest of the schema hangs off,
and enough to prove the migration + async-session roundtrip end to end. The
``VoiceModel`` and ``CallRecord`` tables from agents/gateway.agent.md are
intentionally deferred to the milestone that first reads them (M3 / M5), rather
than standing up tables nothing consumes yet.
"""

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime
from sqlmodel import Field, SQLModel


class Plan(StrEnum):
    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class User(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    email: str = Field(unique=True, index=True)
    display_name: str
    plan: Plan = Plan.FREE
    monthly_minutes_used: float = 0.0
    twilio_phone_number: str | None = None
    settings: dict = Field(default_factory=dict, sa_type=JSON)
    # timezone=True -> Postgres TIMESTAMPTZ, so timezone-aware UTC values store
    # cleanly via asyncpg (a naive column rejects aware datetimes).
    created_at: datetime = Field(default_factory=_utcnow, sa_type=DateTime(timezone=True))
    # onupdate keeps this current on every UPDATE; without it the column would
    # stay frozen at insert time despite the name. Client-side, so no DDL change.
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"onupdate": _utcnow},
    )
