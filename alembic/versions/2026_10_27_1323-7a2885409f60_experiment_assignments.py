"""experiment assignments

Experiments assign each respondent a project on the backend, so the
respondent cannot pick one by editing a link:

- `experiment_assignment`: one row per respondent (a browser, by the hash of
  its `respondent` cookie, or a known participant's pid) and experiment, with
  the project drawn and the latest interview started from it.
- `experiment.allocation`: random by weight (the default, and what existing
  experiments keep), or balanced on assignments or on completed interviews.
- `experiment_project.target`: completed interviews wanted from the project,
  NULL for no limit.
- `project.experiment_only`: refuse respondents no experiment assigned there.
  False for every existing project, so their links keep working.

No experiment has assignments yet: nothing to backfill. Reversible as it
stands.

Revision ID: 7a2885409f60
Revises: 97f96a9a4375
Create Date: 2026-10-27 13:23:59.947257

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
revision: str = "7a2885409f60"
down_revision: str | None = "97f96a9a4375"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "experiment_assignment",
        sa.Column("experiment_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("respondent_hash", sa.String(), nullable=True),
        sa.Column("pid", sa.String(), nullable=True),
        sa.Column("interview_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("entered_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiment.id"],
            name=op.f("fk_experiment_assignment_experiment_id_experiment"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["interview_id"],
            ["interview.id"],
            name=op.f("fk_experiment_assignment_interview_id_interview"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_experiment_assignment_project_id_project"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_experiment_assignment")),
        sa.UniqueConstraint(
            "experiment_id", "pid", name="uq_experiment_assignment_pid"
        ),
        sa.UniqueConstraint("id", name=op.f("uq_experiment_assignment_id")),
    )
    with op.batch_alter_table("experiment_assignment", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_experiment_assignment_experiment_id"),
            ["experiment_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_experiment_assignment_interview_id"),
            ["interview_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_experiment_assignment_project_id"),
            ["project_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_experiment_assignment_respondent_hash"),
            ["respondent_hash"],
            unique=False,
        )

    with op.batch_alter_table("experiment", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "allocation",
                sa.Enum(
                    "RANDOM",
                    "BALANCED",
                    "BALANCED_COMPLETED",
                    name="experimentallocation",
                ),
                server_default="RANDOM",
                nullable=False,
            )
        )

    with op.batch_alter_table("experiment_project", schema=None) as batch_op:
        batch_op.add_column(sa.Column("target", sa.Integer(), nullable=True))

    with op.batch_alter_table("project", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "experiment_only",
                sa.Boolean(),
                server_default=sa.text("0"),
                nullable=False,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("project", schema=None) as batch_op:
        batch_op.drop_column("experiment_only")

    with op.batch_alter_table("experiment_project", schema=None) as batch_op:
        batch_op.drop_column("target")

    with op.batch_alter_table("experiment", schema=None) as batch_op:
        batch_op.drop_column("allocation")

    with op.batch_alter_table("experiment_assignment", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_experiment_assignment_respondent_hash"))
        batch_op.drop_index(batch_op.f("ix_experiment_assignment_project_id"))
        batch_op.drop_index(batch_op.f("ix_experiment_assignment_interview_id"))
        batch_op.drop_index(batch_op.f("ix_experiment_assignment_experiment_id"))

    op.drop_table("experiment_assignment")
