"""message images by name

Revision ID: 9b4e2f7a6c31
Revises: 73a56e59d7a1
Create Date: 2026-11-25 12:00:00.000000

A guide image used to go out base64-encoded in the message, and was stored
with it in `message.image.data`, one copy per interview that asked the
question. Images now go out by name and the browser loads the file
(`app/api/dashboard/guide_images.py`), and the library's `Image` has no
`data` any more, so the stored copies are dead weight: this removes `data`
and keeps the rest (name, description, alt, primer). Not reversible, and
nothing would read it back.

The database file keeps its size until a `VACUUM`.
"""

# NOTE: Don't manage the touch-last_updated triggers here. alembic/env.py
# uninstalls them before every run and reinstalls the full set from
# app/db/triggers.py after, so batch_alter_table on `project`, `testrun` etc.
# cannot lose or break them.
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9b4e2f7a6c31"
down_revision: str | None = "73a56e59d7a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE message SET image = json_remove(image, '$.data') "
            "WHERE json_type(image) = 'object' "
            "AND json_type(image, '$.data') IS NOT NULL"
        )
    )


def downgrade() -> None:
    pass
