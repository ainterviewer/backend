"""widen message.message_type for SECURITY_OVERRIDE

`message_type` is a SQLAlchemy `Enum`, which SQLite stores as a VARCHAR sized
to the longest member name when the table was created. That was CUSTOM_TOKEN,
so every existing SQLite database has VARCHAR(12) -- and SECURITY_OVERRIDE,
added in 7f77bc35ec78, is 17 characters.

Nothing was broken by it, as SQLite does not enforce VARCHAR lengths: the new
value is stored and read back whole. But the models now say VARCHAR(17), so
`alembic check` failed against every existing database, and the next
autogenerate would have picked the change up unasked. This records it.

SQLite cannot change a column's type in place, so batch mode rebuilds the
`message` table -- copying every row, and recreating its indexes and
constraints. PostgreSQL is left alone: there the column is a native enum,
which 7f77bc35ec78 already extended.

Revision ID: cebbae95f61d
Revises: db6f123a226d
Create Date: 2026-09-24 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "cebbae95f61d"
down_revision: str | None = "db6f123a226d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# `SQLEnum` stores the member *names*, and it is their length that sizes the
# column. Frozen here rather than read off the library's enum, so this revision
# means the same thing whatever the enum becomes.
BEFORE = ("TEXT", "IMAGE", "AUDIO", "CUSTOM_TOKEN", "SURVEY_ITEM")
AFTER = (*BEFORE, "SECURITY_OVERRIDE")


def _alter(existing: tuple[str, ...], new: tuple[str, ...]) -> None:
    if op.get_bind().dialect.name == "postgresql":
        return

    with op.batch_alter_table("message", schema=None) as batch_op:
        batch_op.alter_column(
            "message_type",
            existing_type=sa.Enum(*existing, name="messagetype"),
            type_=sa.Enum(*new, name="messagetype"),
            existing_nullable=False,
        )


def upgrade() -> None:
    _alter(BEFORE, AFTER)


def downgrade() -> None:
    # Any SECURITY_OVERRIDE rows outlive the narrower column, as SQLite does not
    # enforce its length.
    _alter(AFTER, BEFORE)
