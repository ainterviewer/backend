"""refresh token used_at

A nullable `used_at` on `refresh_token`: when the token was spent. `/refresh`
lets a token spent moments ago be spent again without counting as reuse, since
one browser routinely refreshes several times at once with the same token, and
needs to know *when* it was spent to tell that from a replay.

Tokens spent before this column existed keep NULL, which reads as "spent long
ago": reusing one still revokes its family, as it did. Reversible as it stands.

Revision ID: 97f96a9a4375
Revises: 8c9dc147b75f
Create Date: 2026-10-23 13:19:06.047954

"""

# NOTE: Don't manage the touch-last_updated triggers here. alembic/env.py
# uninstalls them before every run and reinstalls the full set from
# app/db/triggers.py after, so batch_alter_table on `project`, `testrun` etc.
# cannot lose or break them.
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.types

# revision identifiers, used by Alembic.
revision: str = "97f96a9a4375"
down_revision: str | None = "8c9dc147b75f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "refresh_token",
        sa.Column("used_at", app.db.types.AppDateTime(), nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table("refresh_token", schema=None) as batch_op:
        batch_op.drop_column("used_at")
