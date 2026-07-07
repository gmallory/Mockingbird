"""Gateway database models (SQLModel).

``User`` (M2) is the FK root that the rest of the schema hangs off; its ``id`` is
the Supabase auth user id (``sub``), mirrored locally on first authenticated
request (M6a). ``Voice`` (M4b) is the voice registry: one row per cloned voice,
persisted so a speaker can be rendered as it on the streaming path. As of M6a each
voice is owned by a ``User`` (``user_id`` FK). ``CallRecord`` (M8a) is the
call history: one row per outbound PSTN call placed through Twilio.
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


class CallDirection(StrEnum):
    OUTBOUND = "outbound"
    INBOUND = "inbound"  # reserved for M8b (dedicated inbound numbers)


class CallStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"


# By default SQLAlchemy stores a Python enum by member *name* ("FREE"); pin it to
# the lowercase *value* ("free") so the PG enum labels follow Postgres convention
# and match raw SQL / cross-service reads instead of the Python identifiers.
def _enum_column(enum_cls: type[StrEnum], name: str) -> Column:
    return Column(
        SAEnum(enum_cls, name=name, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )


def _plan_column() -> Column:
    return _enum_column(Plan, "plan")


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


class CallRecord(SQLModel, table=True):
    """One outbound PSTN call placed through Twilio (M8a).

    ``id`` doubles as the media-bridge call id: it names the ``/ws/twilio/{id}``
    stream endpoint in the TwiML and the ``join_call`` target for the browser
    session. ``voice_id`` is the registry row whose voice the call was placed
    with (nullable — an echo/passthrough call has none). Lifecycle: ``active``
    on create, then ``completed``/``failed`` via the Twilio status callback or
    an explicit hangup.
    """

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="user.id", index=True)
    voice_id: UUID | None = Field(default=None, foreign_key="voice.id")
    direction: CallDirection = Field(
        default=CallDirection.OUTBOUND,
        sa_column=_enum_column(CallDirection, "calldirection"),
    )
    status: CallStatus = Field(
        default=CallStatus.ACTIVE,
        sa_column=_enum_column(CallStatus, "callstatus"),
    )
    phone_number: str
    # Twilio's call SID, set once the REST create succeeds; the status callback
    # correlates on it. Indexed for that webhook lookup.
    twilio_call_sid: str | None = Field(default=None, index=True)
    started_at: datetime = Field(default_factory=_utcnow, sa_type=DateTime(timezone=True))
    ended_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    duration_sec: float = 0.0
