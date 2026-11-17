"""version the interview config

The interview config (consent and welcome screens, audio answers, probing
strategies) moves from `project` into the versioned content, so a change to it
is a draft change that reaches respondents when published -- as the agent
configs do -- instead of reaching them the moment it is saved.

Every existing version gets the project's current config: what each held when
it was written was never recorded, and the current one is what interviews on
the published version have been running with.

Revision ID: 081b6053adbe
Revises: 89d83612cfc5
Create Date: 2026-11-17 11:20:42.707601

"""

# NOTE: Don't manage the touch-last_updated triggers here. alembic/env.py
# uninstalls them before every run and reinstalls the full set from
# app/db/triggers.py after, so batch_alter_table on `project`, `testrun` etc.
# cannot lose or break them.
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.types  # noqa: F401

# revision identifiers, used by Alembic.
revision: str = "081b6053adbe"
down_revision: str | None = "89d83612cfc5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: InterviewConfig's defaults, for a row with nothing to copy.
DEFAULT_CONFIG = '{"with_consent": true, "with_welcome": true, "with_audio": false}'


def upgrade() -> None:
    with op.batch_alter_table("projectcontent", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "settings_revision",
                sa.Integer(),
                server_default=sa.text("1"),
                nullable=False,
            )
        )
        batch_op.add_column(sa.Column("config", sa.JSON(), nullable=True))

    with op.batch_alter_table("projectcontentversion", schema=None) as batch_op:
        batch_op.add_column(sa.Column("config", sa.JSON(), nullable=True))

    for table in ("projectcontent", "projectcontentversion"):
        op.execute(
            f"UPDATE {table} SET config = "
            "(SELECT project.config FROM project WHERE project.id = "
            f"{table}.project_id)"
        )
        op.execute(
            sa.text(f"UPDATE {table} SET config = :d WHERE config IS NULL").bindparams(
                d=DEFAULT_CONFIG
            )
        )
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.alter_column("config", existing_type=sa.JSON(), nullable=False)

    with op.batch_alter_table("project", schema=None) as batch_op:
        batch_op.drop_column("config")


def downgrade() -> None:
    with op.batch_alter_table("project", schema=None) as batch_op:
        batch_op.add_column(sa.Column("config", sa.JSON(), nullable=True))

    # The draft's: the closest there is to the one config a project had.
    op.execute(
        "UPDATE project SET config = (SELECT projectcontent.config FROM "
        "projectcontent WHERE projectcontent.project_id = project.id)"
    )
    op.execute(
        sa.text("UPDATE project SET config = :d WHERE config IS NULL").bindparams(
            d=DEFAULT_CONFIG
        )
    )
    with op.batch_alter_table("project", schema=None) as batch_op:
        batch_op.alter_column("config", existing_type=sa.JSON(), nullable=False)

    with op.batch_alter_table("projectcontentversion", schema=None) as batch_op:
        batch_op.drop_column("config")

    with op.batch_alter_table("projectcontent", schema=None) as batch_op:
        batch_op.drop_column("config")
        batch_op.drop_column("settings_revision")
