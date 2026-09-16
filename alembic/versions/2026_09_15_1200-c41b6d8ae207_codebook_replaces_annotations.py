"""replace tag/score annotations with the codebook

The old coding surface was a flat list of `analysis_category` rows -- each a
tag or a score -- applied to whole messages through `message_annotation` and
its `annotation_value` children. It is replaced by a codebook: a tree of
`code` rows (group, tag or score) whose applications are `coding` rows that
may name a span of the message rather than all of it.

Three things the old schema could not say, and the new one can:

* **Structure.** A codebook is a tree, and a flat list of categories made
  every hierarchy something an analyst kept in their head or in the names.
  `code.parent_id` plus `code.rank` carries it, and `GROUP` marks a code that
  organises a branch without ever being applied -- so a parent's count is its
  own, not a sum that pretends a passage coded `Cost` is also coded
  `Barriers`.
* **Passages.** `start_offset`/`end_offset` are character offsets into the
  message content and are both NULL when the whole message is coded. "This
  turn is about cost" and "these nine words are about cost" are different
  claims, which is why the span is nullable rather than 0/len.
* **Scores that stay scores.** `annotation_value.value_int` carried both a
  score's number and a tag's meaningless 1. `coding.value_int` is NULL on a
  tag and a real number on a score, checked against the code's own range.

**The old data is dropped, not migrated.** Categories map onto codes cleanly
enough, but their annotations are the user's coding and the decision to keep
or discard them belongs to them; asked, they chose to start over. This is
therefore irreversible in the sense that matters: `downgrade` rebuilds the
three tables empty, and nothing can bring the rows back.

`project.codebook_palette` holds the colours a code is chosen from. It is the
one part of the codebook that is not a code, and it lives on the project
because an analyst edits it with nothing selected and a colour dropped from it
must survive on the codes already painted with it. NULL means "never saved",
which the repository reads as the default palette.

Revision ID: c41b6d8ae207
Revises: a3d7c05e91b4
Create Date: 2026-09-15 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c41b6d8ae207"
down_revision: str | None = "a3d7c05e91b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "code",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("project_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("parent_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("definition", sa.Text(), nullable=False),
        sa.Column("memo", sa.Text(), nullable=False),
        sa.Column("color", sa.String(), nullable=False),
        # `SQLEnum` stores the member's *name*, so these are the strings in the
        # column -- see `app.db.types.CodeKind`.
        sa.Column(
            "kind",
            sa.Enum("GROUP", "TAG", "SCORE", name="codekind"),
            nullable=False,
        ),
        sa.Column("min_value", sa.Integer(), nullable=True),
        sa.Column("max_value", sa.Integer(), nullable=True),
        sa.Column("position_x", sa.Float(), nullable=True),
        sa.Column("position_y", sa.Float(), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["code.id"],
            name=op.f("fk_code_parent_id_code"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_code_project_id_project"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_code")),
        sa.UniqueConstraint("id", name=op.f("uq_code_id")),
    )
    op.create_index("ix_code_project_id", "code", ["project_id"], unique=False)
    op.create_index(op.f("ix_code_parent_id"), "code", ["parent_id"], unique=False)

    op.create_table(
        "coding",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("code_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("message_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        # Both NULL means the whole message; see the module docstring.
        sa.Column("start_offset", sa.Integer(), nullable=True),
        sa.Column("end_offset", sa.Integer(), nullable=True),
        sa.Column("value_int", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["code_id"],
            ["code.id"],
            name=op.f("fk_coding_code_id_code"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["message.id"],
            name=op.f("fk_coding_message_id_message"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_coding_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_coding")),
        sa.UniqueConstraint("id", name=op.f("uq_coding_id")),
    )
    op.create_index("ix_coding_message_id", "coding", ["message_id"], unique=False)
    op.create_index("ix_coding_code_id", "coding", ["code_id"], unique=False)

    # Not `batch_alter_table`: batch mode rebuilds the table under a temporary
    # name, and `project` is referenced by name from the triggers in
    # `app.db.triggers`, which then fail mid-rename. SQLite has supported
    # adding and dropping a column in place since 3.35.
    op.add_column("project", sa.Column("codebook_palette", sa.JSON(), nullable=True))

    # Children first: the app does not enforce foreign keys on SQLite, so
    # dropping the parent would leave these behind on a database that does.
    op.drop_table("annotation_value")
    op.drop_table("message_annotation")
    op.drop_table("analysis_category")


def downgrade() -> None:
    """Rebuild the old tables, empty.

    The rows they held are gone -- see the module docstring. This exists so
    that a database can be walked back to the previous schema, not so that the
    previous coding can be recovered.
    """
    op.create_table(
        "analysis_category",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("project_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "type", sa.Enum("TAG", "SCORE", name="annotationtype"), nullable=False
        ),
        sa.Column("color", sa.String(), nullable=False),
        sa.Column("min_value", sa.Integer(), nullable=True),
        sa.Column("max_value", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_analysis_category_project_id_project"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analysis_category")),
        sa.UniqueConstraint("id", name=op.f("uq_analysis_category_id")),
        sa.UniqueConstraint("project_id", "name", name="unique_project_category_name"),
    )
    op.create_table(
        "message_annotation",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("message_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["message.id"],
            name=op.f("fk_message_annotation_message_id_message"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["user.id"], name=op.f("fk_message_annotation_user_id_user")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message_annotation")),
        sa.UniqueConstraint("id", name=op.f("uq_message_annotation_id")),
    )
    op.create_table(
        "annotation_value",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("annotation_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("category_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("value_int", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["annotation_id"],
            ["message_annotation.id"],
            name=op.f("fk_annotation_value_annotation_id_message_annotation"),
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["analysis_category.id"],
            name=op.f("fk_annotation_value_category_id_analysis_category"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_annotation_value")),
        sa.UniqueConstraint("id", name=op.f("uq_annotation_value_id")),
        sa.UniqueConstraint(
            "annotation_id", "category_id", name="unique_annotation_category"
        ),
    )

    op.drop_column("project", "codebook_palette")

    op.drop_index("ix_coding_code_id", table_name="coding")
    op.drop_index("ix_coding_message_id", table_name="coding")
    op.drop_table("coding")
    op.drop_index(op.f("ix_code_parent_id"), table_name="code")
    op.drop_index("ix_code_project_id", table_name="code")
    op.drop_table("code")
