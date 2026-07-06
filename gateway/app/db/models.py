"""Gateway database models (SQLModel).

``User`` (M2) is the FK root that the rest of the schema hangs off; its ``id`` is
the Supabase auth user id (``sub``), mirrored locally on first authenticated
request (M6a). ``Voice`` (M4b) is the voice registry: one row per cloned voice,
persisted so a speaker can be rendered as it on the streaming path. As of M6a each
voice is owned by a ``User`` (``user_id`` FK); the ``CallRecord`` table from
agents/gateway.agent.md is still deferred to the milestone that first reads it (M8).
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
    # ``id`` is the Supabase auth user id (``sub``); this row is a *mirror* of the
    # Supabase identity, materialized on first authenticated request (M6a).
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    # Indexed for lookups but intentionally NOT unique: Supabase owns identity and
    # its email policy, so the mirror must not impose a second uniqueness rule that
    # could turn an otherwise-valid token into a write conflict. Email is a cached
    # display attribute here; the ``sub`` (id) is the identity.
    email: str = Field(index=True)
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
    """A cloned voice in the registry (M4b), owned by a ``User`` (M6a).

    ``voice_id`` is the id minted by the inference clone route (Cartesia voice id
    or self-hosted ONNX model id); it feeds straight into the streaming path when
    this voice is selected. ``label`` is the human name shown in the UI (M4c).
    ``user_id`` scopes the registry per user: ``GET /voices`` lists only the
    caller's rows and ``POST /voices`` stamps the authenticated caller as owner.
    """

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    # Owner (Supabase auth id, mirrored in ``user``). Indexed because every list
    # query filters on it. Not nullable: a voice always belongs to someone.
    user_id: UUID = Field(foreign_key="user.id", index=True)
    voice_id: str = Field(unique=True, index=True)
    label: str
    language: str
    created_at: datetime = Field(default_factory=_utcnow, sa_type=DateTime(timezone=True))
