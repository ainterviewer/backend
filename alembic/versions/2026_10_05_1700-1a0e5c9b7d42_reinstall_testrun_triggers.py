"""reinstall the testrun triggers

e5f2a91c4d80 (shipped in v0.4.31) batch-altered `testrun` to change its
foreign keys without reinstalling the touch-last_updated triggers. On SQLite
that recreates the table and drops every trigger on it, so prod and staging
lost the six that bump testsetup.last_updated and project.last_updated when a
test run changes, and ran without them for six weeks. (This was first blamed
on c7bde657f2c4, whose first version had the same flaw, but the backups show
the triggers were already gone before it ran.)

This puts them back. `install_triggers` drops and recreates every trigger, so it
is a no-op wherever they are already in place. On main, alembic/env.py now
reinstalls the triggers after every run, so this cannot recur.

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
