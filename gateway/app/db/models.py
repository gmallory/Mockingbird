"""Gateway database models (SQLModel).

``User`` (M2) is the FK root that the rest of the schema hangs off. ``Voice`` (M4b)
is the single-user voice registry: one row per cloned Cartesia voice, persisted so
a speaker can be rendered as it on the streaming path. Per-user ownership (an FK to
``User``) and the ``CallRecord`` table from agents/gateway.agent.md are deferred to
the milestone that first reads them (M5 auth / M8 calling).
"""

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import JSON, Column, DateTime
from sqlalchemy import Enum as SAEnum
from sqlalchemy.ext.mutable import MutableDict
from sqlmodel import Field, SQLModel


class Plan(StrEnum):
    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"


# By default SQLAlchemy stores a Python enum by member *name* ("FREE"); pin it to
# the lowercase *value* ("free") so the PG enum labels follow Postgres convention
# and match raw SQL / cross-service reads instead of the Python identifiers.
def _plan_column() -> Column:
    return Column(
        SAEnum(Plan, name="plan", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)


class User(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    email: str = Field(unique=True, index=True)
    display_name: str
    plan: Plan = Field(default=Plan.FREE, sa_column=_plan_column())
    monthly_minutes_used: float = 0.0
    twilio_phone_number: str | None = None
    # MutableDict so in-place edits (settings["k"] = v) are tracked and flushed;
    # a bare JSON column only persists a wholesale reassignment.
    settings: dict = Field(
        default_factory=dict,
        sa_column=Column(MutableDict.as_mutable(JSON), nullable=False),
    )
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


class Voice(SQLModel, table=True):
    """A cloned voice in the single-user registry (M4b).

    ``voice_id`` is the Cartesia id minted by the inference clone route; it feeds
    straight into the voice-changer ``voice[id]`` when this voice is selected.
    ``label`` is the human name shown in the UI (M4c).
    """

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    voice_id: str = Field(unique=True, index=True)
    label: str
    language: str
    created_at: datetime = Field(default_factory=_utcnow, sa_type=DateTime(timezone=True))
