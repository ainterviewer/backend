"""add started_by_id to testrun

Records which user started a synthetic test run, so the number of synthetic
interviews a demo user has in flight can be capped per user rather than per
project owner. Existing runs are left null: who started them was never stored.

Shipped as a hotfix on release/0.4.x, so it hangs off that line's head
(d4f83a01c96b). On main the same revision sits beside the later migrations and
a merge revision joins the two, so a database at this revision can still be
upgraded to main.

Revision ID: c7bde657f2c4
Revises: d4f83a01c96b
Create Date: 2026-10-02 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7bde657f2c4"
down_revision: str | None = "d4f83a01c96b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

_CONSTRAINT = "fk_testrun_started_by_id_user"
_INDEX = "ix_testrun_started_by_id"


def upgrade() -> None:
    with op.batch_alter_table(
        "testrun", naming_convention=NAMING_CONVENTION
    ) as batch_op:
        batch_op.add_column(sa.Column("started_by_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            _CONSTRAINT, "user", ["started_by_id"], ["id"], ondelete="SET NULL"
        )
        batch_op.create_index(_INDEX, ["started_by_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table(
        "testrun", naming_convention=NAMING_CONVENTION
    ) as batch_op:
        batch_op.drop_index(_INDEX)
        batch_op.drop_constraint(_CONSTRAINT, type_="foreignkey")
        batch_op.drop_column("started_by_id")
