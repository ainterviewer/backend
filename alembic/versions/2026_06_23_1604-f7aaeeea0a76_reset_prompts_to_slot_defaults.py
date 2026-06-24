"""reset prompts to slot defaults

Revision ID: f7aaeeea0a76
Revises: d59584c07ecb
Create Date: 2026-06-23 16:04:35.567469

"""

# NOTE: If this migration uses `op.batch_alter_table` against any of `project`,
# `projectlocalization`, `testsetup`, or `testrun` (the tables referenced by
# the touch-last_updated triggers), wrap the upgrade/downgrade bodies with
# `uninstall_triggers(op.get_bind())` before and
# `install_triggers(op.get_bind())` after. SQLite batch-alter renames the
# table, which breaks any trigger that references it by name. See
# `app/db/triggers.py` and revision 3d64d3a385a1 for an example.
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

import app.db.types  # noqa: F401

from ainterviewer.agents.prompts.models import DEFAULT_PROMPTS


# revision identifiers, used by Alembic.
revision: str = "f7aaeeea0a76"
down_revision: Union[str, None] = "d59584c07ecb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The probing-agent system/instruction templates were refactored into editable
# "slots" (see ainterviewer.agents.config.ProbingPromptSlots). Stored full-text
# templates predate that refactor and would shadow the new slot-aware package
# templates, so every project's prompts are reset to the current defaults. User
# customizations now live on AgentConfigs.probing.prompt_slots instead.
projectlocalization = sa.table(
    "projectlocalization",
    sa.column("prompts", sa.JSON()),
)


def upgrade() -> None:
    op.execute(
        projectlocalization.update().values(
            prompts=DEFAULT_PROMPTS.model_dump(mode="json")
        )
    )


def downgrade() -> None:
    # Irreversible data migration: the prior per-project template text is not
    # recoverable. Defaults remain in place on downgrade.
    pass
