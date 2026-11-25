"""record what a cloned project was copied from

Two nullable columns on `project`, both SET NULL when what they name goes.
No data: projects copied before this have no recorded source.

Revision ID: 4ccbc77850d9
Revises: 9b4e2f7a6c31
Create Date: 2026-11-20 15:39:38.845437

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
revision: str = "4ccbc77850d9"
down_revision: str | None = "9b4e2f7a6c31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("project", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("cloned_from_project_id", sa.Uuid(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("cloned_from_version_id", sa.Uuid(), nullable=True)
        )
        batch_op.create_index(
            batch_op.f("ix_project_cloned_from_project_id"),
            ["cloned_from_project_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_project_cloned_from_version_id"),
            ["cloned_from_version_id"],
            unique=False,
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_project_cloned_from_project_id_project"),
            "project",
            ["cloned_from_project_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_project_cloned_from_version_id_projectcontentversion"),
            "projectcontentversion",
            ["cloned_from_version_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("project", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_project_cloned_from_version_id_projectcontentversion"),
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            batch_op.f("fk_project_cloned_from_project_id_project"), type_="foreignkey"
        )
        batch_op.drop_index(batch_op.f("ix_project_cloned_from_version_id"))
        batch_op.drop_index(batch_op.f("ix_project_cloned_from_project_id"))
        batch_op.drop_column("cloned_from_version_id")
        batch_op.drop_column("cloned_from_project_id")
