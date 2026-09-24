"""add security_intervention to message

Marks a message sent by the interview's security check, so the respondent is
shown it in a modal rather than in the chat -- also when the interview is
resumed and its history replayed. NULL on every other message, which is every
message written before this revision.

The respondent's answer to an intervention they may override is stored as a
message of its own, with the new SECURITY_OVERRIDE member of the messagetype
enum -- a native type on PostgreSQL, where it has to be added explicitly.

Revision ID: 7f77bc35ec78
Revises: c41b6d8ae207
Create Date: 2026-09-24 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7f77bc35ec78"
down_revision: str | None = "c41b6d8ae207"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("message", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("security_intervention", sa.JSON(), nullable=True)
        )

    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TYPE messagetype ADD VALUE IF NOT EXISTS 'SECURITY_OVERRIDE'")


def downgrade() -> None:
    # The SECURITY_OVERRIDE enum value is left in place on PostgreSQL: removing
    # an enum value requires rebuilding the type and any rows using it.
    with op.batch_alter_table("message", schema=None) as batch_op:
        batch_op.drop_column("security_intervention")
