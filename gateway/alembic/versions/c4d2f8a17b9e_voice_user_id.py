"""voice.user_id owner FK (M6a)

Revision ID: c4d2f8a17b9e
Revises: b3f1a6c9d2e4
Create Date: 2026-07-05 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4d2f8a17b9e"
down_revision: str | Sequence[str] | None = "b3f1a6c9d2e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Add the owner FK to ``voice``. Any pre-M6a rows predate auth and so have no
    ownable ``user`` to point at (no users existed before this milestone); they
    are dev-registry artifacts (throwaway clone ids), so we clear them before
    adding the NOT NULL column rather than inventing an owner. This keeps the
    migration deterministic on a populated dev database.
    """
    op.execute("DELETE FROM voice")
    op.add_column("voice", sa.Column("user_id", sa.Uuid(), nullable=False))
    op.create_index(op.f("ix_voice_user_id"), "voice", ["user_id"], unique=False)
    op.create_foreign_key(op.f("fk_voice_user_id_user"), "voice", "user", ["user_id"], ["id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f("fk_voice_user_id_user"), "voice", type_="foreignkey")
    op.drop_index(op.f("ix_voice_user_id"), table_name="voice")
    op.drop_column("voice", "user_id")
