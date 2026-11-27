"""add suggested_probe to message

Marks a probe asked from one of its question's suggested probes, under
reword or strict probe steering, with that suggestion. A reworded probe does
not read as its suggestion, so this is what keeps a resumed interview from
asking it again. NULL on every other message, which is every message written
before this revision: no probe was steered before it.

Revision ID: a3d6f19c0e42
Revises: e5b8d03a7f12
Create Date: 2026-11-28 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3d6f19c0e42"
down_revision: str | None = "e5b8d03a7f12"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("message", schema=None) as batch_op:
        batch_op.add_column(sa.Column("suggested_probe", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("message", schema=None) as batch_op:
        batch_op.drop_column("suggested_probe")
