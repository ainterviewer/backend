"""touch_project_on_localization_change

Revision ID: 9c7f4f2f91ab
Revises: 8804ba71199e
Create Date: 2026-04-27 12:15:00.000000

Trigger SQL lives in ``app/db/triggers.py``. This migration just (re)installs
whatever is defined there at the time it runs.
"""

from typing import Sequence, Union

from alembic import op

from app.db.triggers import install_triggers, uninstall_triggers

# revision identifiers, used by Alembic.
revision: str = "9c7f4f2f91ab"
down_revision: Union[str, None] = "8804ba71199e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    install_triggers(op.get_bind())


def downgrade() -> None:
    uninstall_triggers(op.get_bind())
