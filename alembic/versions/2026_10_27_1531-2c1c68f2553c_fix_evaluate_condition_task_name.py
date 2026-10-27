"""fix evaluate_condition task name

The library wrote condition evaluations as `evaulate_condition` until v0.5.0
fixed the typo, so rows from before then carry the old spelling and the
report, which counts `evaluate_condition`, missed them. Renames them.

Not reversible: once renamed, nothing tells which rows had the typo, and
nothing reads the old spelling any more. The downgrade leaves them as they are.

Revision ID: 2c1c68f2553c
Revises: 7a2885409f60
Create Date: 2026-10-27 15:31:10.013852

"""

# NOTE: Don't manage the touch-last_updated triggers here. alembic/env.py
# uninstalls them before every run and reinstalls the full set from
# app/db/triggers.py after, so batch_alter_table on `project`, `testrun` etc.
# cannot lose or break them.
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.types  # noqa: F401

# revision identifiers, used by Alembic.
revision: str = "2c1c68f2553c"
down_revision: str | None = "7a2885409f60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

task = sa.table("task", sa.column("task", sa.Text()))


def upgrade() -> None:
    op.execute(
        task.update()
        .where(task.c.task == "evaulate_condition")
        .values(task="evaluate_condition")
    )


def downgrade() -> None:
    pass
