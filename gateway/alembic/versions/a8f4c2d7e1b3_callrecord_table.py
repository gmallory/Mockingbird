"""callrecord table (M8a)

Revision ID: a8f4c2d7e1b3
Revises: d1e5a9c3f7b2
Create Date: 2026-07-07 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a8f4c2d7e1b3"
down_revision: str | Sequence[str] | None = "d1e5a9c3f7b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    The enum types are created explicitly with ``checkfirst`` (and referenced
    with ``create_type=False`` below) so the migration is idempotent against a
    dev database where a test-suite ``create_all`` already minted them.
    """
    direction = postgresql.ENUM("outbound", "inbound", name="calldirection", create_type=False)
    status = postgresql.ENUM("active", "completed", "failed", name="callstatus", create_type=False)
    direction.create(op.get_bind(), checkfirst=True)
    status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "callrecord",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("voice_id", sa.Uuid(), nullable=True),
        sa.Column("direction", direction, nullable=False),
        sa.Column("status", status, nullable=False),
        sa.Column("phone_number", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("twilio_call_sid", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_sec", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["voice_id"], ["voice.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_callrecord_user_id"), "callrecord", ["user_id"], unique=False)
    op.create_index(
        op.f("ix_callrecord_twilio_call_sid"), "callrecord", ["twilio_call_sid"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_callrecord_twilio_call_sid"), table_name="callrecord")
    op.drop_index(op.f("ix_callrecord_user_id"), table_name="callrecord")
    op.drop_table("callrecord")
    sa.Enum(name="calldirection").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="callstatus").drop(op.get_bind(), checkfirst=True)
