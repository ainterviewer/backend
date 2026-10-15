"""content docs

`guidedoc` becomes `contentdoc`: one collaborative document per project and
live part (guide, consent, welcome), so the consent and welcome pages can be
co-edited like the guide. Existing guide docs move across as the `GUIDE`
part, their `guide_revision` as `revision`; consent and welcome docs are
seeded from their columns when an editor first opens them. The downgrade
keeps the guide docs and drops the others (their content is in
`projectcontent`).

Revision ID: d874205f7f00
Revises: c6ee9328d284
Create Date: 2026-10-15 18:03:00

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
revision: str = "d874205f7f00"
down_revision: str | None = "c6ee9328d284"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PARTS = sa.Enum(
    "GUIDE",
    "CONSENT",
    "WELCOME",
    "AGENTS",
    "EMAILS",
    "LANGUAGES",
    name="contentpart",
)


def upgrade() -> None:
    op.create_table(
        "contentdoc",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("part", _PARTS, nullable=False),
        sa.Column("state", sa.LargeBinary(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("errors", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_contentdoc_project_id_project"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contentdoc")),
        sa.UniqueConstraint("id", name=op.f("uq_contentdoc_id")),
        sa.UniqueConstraint("project_id", "part", name="project_part"),
    )
    with op.batch_alter_table("contentdoc", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_contentdoc_project_id"), ["project_id"], unique=False
        )
    op.execute(
        "INSERT INTO contentdoc "
        "(id, project_id, part, state, revision, errors, updated_at) "
        "SELECT id, project_id, 'GUIDE', state, guide_revision, errors, updated_at "
        "FROM guidedoc"
    )
    with op.batch_alter_table("guidedoc", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_guidedoc_project_id"))
    op.drop_table("guidedoc")


def downgrade() -> None:
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
    op.execute(
        "INSERT INTO guidedoc "
        "(id, project_id, state, guide_revision, errors, updated_at) "
        "SELECT id, project_id, state, revision, errors, updated_at "
        "FROM contentdoc WHERE part = 'GUIDE'"
    )
    with op.batch_alter_table("contentdoc", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_contentdoc_project_id"))
    op.drop_table("contentdoc")
