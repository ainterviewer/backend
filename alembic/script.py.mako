"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
# NOTE: If this migration uses `op.batch_alter_table` against any of `project`,
# `projectlocalization`, `testsetup`, or `testrun` (the tables referenced by
# the touch-last_updated triggers), wrap the upgrade/downgrade bodies with
# `uninstall_triggers(op.get_bind())` before and
# `install_triggers(op.get_bind())` after. SQLite batch-alter renames the
# table, which breaks any trigger that references it by name. See
# `app/db/triggers.py` and revision 3d64d3a385a1 for an example.
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa # noqa: F401

import app.db.types # noqa: F401
${imports if imports else ""}

# revision identifiers, used by Alembic.
revision: str = ${repr(up_revision)}
down_revision: Union[str, None] = ${repr(down_revision)}
branch_labels: Union[str, Sequence[str], None] = ${repr(branch_labels)}
depends_on: Union[str, Sequence[str], None] = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
