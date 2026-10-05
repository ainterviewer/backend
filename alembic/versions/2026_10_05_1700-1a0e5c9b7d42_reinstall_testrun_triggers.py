"""reinstall the testrun triggers

The first version of c7bde657f2c4 batch-altered `testrun` without uninstalling
and reinstalling the touch-last_updated triggers around it. On SQLite that
recreates the table and drops every trigger on it, so databases that ran it
(prod, with v0.4.37) lost the six that bump testsetup.last_updated and
project.last_updated when a test run changes.

c7bde657f2c4 now handles the triggers itself; this puts them back on the
databases that ran it before. `install_triggers` drops and recreates every
trigger, so it is a no-op wherever they are already in place.

Revision ID: 1a0e5c9b7d42
Revises: e2a7c1f09b3d
Create Date: 2026-10-05 17:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

from app.db.triggers import install_triggers

# revision identifiers, used by Alembic.
revision: str = "1a0e5c9b7d42"
down_revision: str | None = "e2a7c1f09b3d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    install_triggers(op.get_bind())


def downgrade() -> None:
    # The triggers belong to every revision below this one too, so there is
    # nothing to undo.
    pass
