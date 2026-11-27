"""rename the contains_multiple_refusals task to contains_refusal

The check judges only the latest answer to a probe, not every answer to the
question, so the old name no longer fits. Rows written before the rename are
renamed with it, so one name covers the task's whole history.

Revision ID: f3a1b4871884
Revises: 4ccbc77850d9
Create Date: 2026-11-27 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3a1b4871884"
down_revision: str | None = "4ccbc77850d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_NAME = "contains_multiple_refusals"
NEW_NAME = "contains_refusal"


def _rename(old: str, new: str) -> None:
    op.get_bind().execute(
        sa.text("UPDATE task SET task = :new WHERE task = :old"),
        {"old": old, "new": new},
    )


def upgrade() -> None:
    _rename(OLD_NAME, NEW_NAME)


def downgrade() -> None:
    _rename(NEW_NAME, OLD_NAME)
