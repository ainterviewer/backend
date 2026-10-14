"""per-part content revisions

One revision counter per part of a project's content (guide, consent,
welcome, agents, emails, languages) beside the overall `revision`, so a save
to one part never conflicts with another. Needed before the guide autosaves:
with one counter, every guide save would make every other editor's next save
a 412.

Every part starts at the row's current `revision`, so an editor loaded
before the upgrade (holding that revision as its ETag) can still save. The
overall `revision` stays: publish and restore check it.

The downgrade drops the columns; the overall revision has kept counting
throughout, so nothing else needs undoing.

Revision ID: 0958d3a3fcbd
Revises: b047e099440e
Create Date: 2026-10-14 17:54:31.483289

"""

# NOTE: Don't manage the touch-last_updated triggers here. alembic/env.py
# uninstalls them before every run and reinstalls the full set from
# app/db/triggers.py after, so batch_alter_table on `project`, `testrun` etc.
# cannot lose or break them.
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0958d3a3fcbd"
down_revision: str | None = "b047e099440e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Frozen here: the columns this revision added, whatever `ContentPart` says
#: later.
COLUMNS = (
    "guide_revision",
    "consent_revision",
    "welcome_revision",
    "agents_revision",
    "emails_revision",
    "languages_revision",
)


def upgrade() -> None:
    with op.batch_alter_table("projectcontent", schema=None) as batch_op:
        for column in COLUMNS:
            batch_op.add_column(
                sa.Column(
                    column,
                    sa.Integer(),
                    server_default=sa.text("1"),
                    nullable=False,
                )
            )
    op.execute(
        "UPDATE projectcontent SET "
        + ", ".join(f"{column} = revision" for column in COLUMNS)
    )


def downgrade() -> None:
    with op.batch_alter_table("projectcontent", schema=None) as batch_op:
        for column in reversed(COLUMNS):
            batch_op.drop_column(column)
