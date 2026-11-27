"""give preview features out one by one

`user.with_preview_features` was one flag for every unfinished feature.
`user.preview_features` lists them (`PreviewFeature` values), so a tester can
be given the report without the codebook. Everyone who had the flag gets
every feature there is now, so nobody loses one; a downgrade gives the flag
back to whoever has any.

Invitations gain `user_preview_features`, handed to who registers with one,
like their demo budgets.

Revision ID: c7e2a9d41b6f
Revises: f3a1b4871884
Create Date: 2026-11-27 13:00:00.000000

"""

# NOTE: Don't manage the touch-last_updated triggers here. alembic/env.py
# uninstalls them before every run and reinstalls the full set from
# app/db/triggers.py after, so batch_alter_table on `project`, `testrun` etc.
# cannot lose or break them.
import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.types  # noqa: F401

# revision identifiers, used by Alembic.
revision: str = "c7e2a9d41b6f"
down_revision: str | None = "f3a1b4871884"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The features at this revision, written out rather than imported: a later
# graduation removes a member, and this migration must keep meaning the same.
_FEATURES = ["audio", "codebook", "participants", "probing", "report", "safety"]


def upgrade() -> None:
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "preview_features", sa.JSON(), nullable=False, server_default="[]"
            )
        )
    op.execute(
        sa.text(
            "UPDATE user SET preview_features = :features WHERE with_preview_features"
        ).bindparams(features=json.dumps(_FEATURES))
    )
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.drop_column("with_preview_features")

    with op.batch_alter_table("invitation", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "user_preview_features",
                sa.JSON(),
                nullable=False,
                server_default="[]",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("invitation", schema=None) as batch_op:
        batch_op.drop_column("user_preview_features")

    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "with_preview_features",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
    op.execute(
        "UPDATE user SET with_preview_features = 1 "
        "WHERE json_array_length(preview_features) > 0"
    )
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.drop_column("preview_features")
