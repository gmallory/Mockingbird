"""voice.voice_id unique constraint

Revision ID: b3f1a6c9d2e4
Revises: e9658781796e
Create Date: 2026-06-30 15:30:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3f1a6c9d2e4"
down_revision: str | Sequence[str] | None = "e9658781796e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index(op.f("ix_voice_voice_id"), "voice", ["voice_id"], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_voice_voice_id"), table_name="voice")
