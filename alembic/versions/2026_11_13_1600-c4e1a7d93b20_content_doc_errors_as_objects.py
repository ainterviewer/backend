"""content doc errors as objects

Revision ID: c4e1a7d93b20
Revises: bd0f9b5d7859
Create Date: 2026-11-13 16:00:00

`contentdoc.errors` held each error as its message; now an object
(`app.types.ContentError`) with the message under `message`.
"""

# NOTE: Don't manage the touch-last_updated triggers here. alembic/env.py
# uninstalls them before every run and reinstalls the full set from
# app/db/triggers.py after, so batch_alter_table on `project`, `testrun` etc.
# cannot lose or break them.
import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4e1a7d93b20"
down_revision: str | None = "bd0f9b5d7859"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DOCS = sa.table("contentdoc", sa.column("id"), sa.column("errors", sa.JSON))


def _convert(convert) -> None:
    bind = op.get_bind()
    for id_, errors in bind.execute(sa.select(_DOCS.c.id, _DOCS.c.errors)).all():
        if isinstance(errors, str):  # JSON read back as text on some drivers
            errors = json.loads(errors)
        if errors:
            bind.execute(
                _DOCS.update()
                .where(_DOCS.c.id == id_)
                .values(errors=[convert(error) for error in errors])
            )


def upgrade() -> None:
    _convert(lambda e: {"message": e} if isinstance(e, str) else e)


def downgrade() -> None:
    _convert(lambda e: e["message"] if isinstance(e, dict) else e)
