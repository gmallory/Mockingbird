"""user.email index not unique (M6a mirror)

Revision ID: d1e5a9c3f7b2
Revises: c4d2f8a17b9e
Create Date: 2026-07-05 12:30:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d1e5a9c3f7b2"
down_revision: str | Sequence[str] | None = "c4d2f8a17b9e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop the unique constraint on ``user.email``, keeping a plain index.

    The ``user`` row mirrors the Supabase identity keyed by ``id`` (the Supabase
    ``sub``); email is a cached attribute. A unique email would let a valid token
    (new ``sub``, reused email) fail the mirror insert, so verification is the sole
    identity authority and email uniqueness moves out of our schema.
    """
    op.drop_index(op.f("ix_user_email"), table_name="user")
    op.create_index(op.f("ix_user_email"), "user", ["email"], unique=False)


def downgrade() -> None:
    """Restore the unique index on ``user.email``."""
    op.drop_index(op.f("ix_user_email"), table_name="user")
    op.create_index(op.f("ix_user_email"), "user", ["email"], unique=True)
