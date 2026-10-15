"""guide docs

The collaborative guide editor's Yjs documents, one per project. Created
empty: a project's doc is seeded from `projectcontent.guide` when an editor
first opens it, so nothing needs migrating, and the downgrade loses only the
doc state (the guide itself is in `projectcontent`).

Revision ID: e82825da6890
Revises: 0958d3a3fcbd
Create Date: 2026-10-14 18:14:21.154870

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
revision: str = "e82825da6890"
down_revision: str | None = "0958d3a3fcbd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "guidedoc",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.LargeBinary(), nullable=False),
        sa.Column("guide_revision", sa.Integer(), nullable=False),
        sa.Column("errors", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_guidedoc_project_id_project"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_guidedoc")),
        sa.UniqueConstraint("id", name=op.f("uq_guidedoc_id")),
    )
    with op.batch_alter_table("guidedoc", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_guidedoc_project_id"), ["project_id"], unique=True
        )


def downgrade() -> None:
    with op.batch_alter_table("guidedoc", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_guidedoc_project_id"))

    op.drop_table("guidedoc")
