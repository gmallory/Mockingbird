"""voicemodel table (M9)

Revision ID: ca561f6a0421
Revises: a8f4c2d7e1b3
Create Date: 2026-07-09 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ca561f6a0421"
down_revision: str | Sequence[str] | None = "a8f4c2d7e1b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    The enum types are created explicitly with ``checkfirst`` (and referenced
    with ``create_type=False`` below) so the migration is idempotent against a
    dev database where a test-suite ``create_all`` already minted them —
    same pattern as ``a8f4c2d7e1b3``.
    """
    voicemodeltype = postgresql.ENUM("instant", "hd", name="voicemodeltype", create_type=False)
    voicemodelstatus = postgresql.ENUM(
        "training", "ready", "failed", name="voicemodelstatus", create_type=False
    )
    voicemodeltype.create(op.get_bind(), checkfirst=True)
    voicemodelstatus.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "voicemodel",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("voice_id", sa.Uuid(), nullable=True),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("type", voicemodeltype, nullable=False),
        sa.Column("status", voicemodelstatus, nullable=False),
        sa.Column("progress", sa.Float(), nullable=False),
        sa.Column("stage", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("error", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("sample_duration_sec", sa.Float(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("training_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("training_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("model_path", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("model_size_bytes", sa.Integer(), nullable=False),
        sa.Column("similarity_score", sa.Float(), nullable=True),
        sa.Column("mos_score", sa.Float(), nullable=True),
        sa.Column("pitch_offset", sa.Float(), nullable=False),
        sa.Column("speed_factor", sa.Float(), nullable=False),
        sa.Column("breathiness", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["voice_id"], ["voice.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_voicemodel_user_id"), "voicemodel", ["user_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_voicemodel_user_id"), table_name="voicemodel")
    op.drop_table("voicemodel")
    sa.Enum(name="voicemodeltype").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="voicemodelstatus").drop(op.get_bind(), checkfirst=True)
