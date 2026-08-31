"""Move message comments into their own threaded table

A comment used to be a column on ``message_annotation``: one row per user per
message, holding both that user's category coding and a single free-text note.
Two people could each leave one, but neither could answer the other.

``message_comment`` makes the discussion its own thing -- several users per
message, and replies via ``parent_id``. Threads are deliberately two levels
deep (a root plus a flat list of replies); the repository rejects a reply to a
reply rather than re-pointing it.

Every existing ``message_annotation.comment`` becomes a root comment carrying
the same author and timestamps, and the column is dropped: leaving it in place
would mean two places to read a message's comments from.

Revision ID: 3c9a1e77b204
Revises: d4f83a01c96b
Create Date: 2026-08-31 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3c9a1e77b204"
down_revision: str | None = "d4f83a01c96b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "message_comment",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("message_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("parent_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["message.id"],
            name=op.f("fk_message_comment_message_id_message"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_message_comment_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["message_comment.id"],
            name=op.f("fk_message_comment_parent_id_message_comment"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message_comment")),
        sa.UniqueConstraint("id", name=op.f("uq_message_comment_id")),
    )
    op.create_index(
        op.f("ix_message_comment_message_id"), "message_comment", ["message_id"]
    )
    # Reading a thread means fetching a root's replies by parent_id.
    op.create_index(
        op.f("ix_message_comment_parent_id"), "message_comment", ["parent_id"]
    )

    # Carry the existing notes over as root comments. The id is generated in
    # SQL so this stays one statement on both SQLite and PostgreSQL; only the
    # annotation's own id is available to derive it from, and it is unique.
    op.execute(
        sa.text(
            """
            INSERT INTO message_comment (
                id, message_id, user_id, parent_id, body, created_at, updated_at
            )
            SELECT id, message_id, user_id, NULL, comment, created_at, updated_at
            FROM message_annotation
            WHERE comment IS NOT NULL AND comment != ''
            """
        )
    )

    with op.batch_alter_table("message_annotation") as batch_op:
        batch_op.drop_column("comment")


def downgrade() -> None:
    with op.batch_alter_table("message_annotation") as batch_op:
        batch_op.add_column(sa.Column("comment", sa.Text(), nullable=True))

    # Only the root comments that came from an annotation can go back: an
    # annotation holds one comment, so replies and any second root on the same
    # message have nowhere to return to and are dropped with the table.
    op.execute(
        sa.text(
            """
            UPDATE message_annotation
            SET comment = (
                SELECT body FROM message_comment
                WHERE message_comment.id = message_annotation.id
            )
            WHERE EXISTS (
                SELECT 1 FROM message_comment
                WHERE message_comment.id = message_annotation.id
            )
            """
        )
    )

    op.drop_index(op.f("ix_message_comment_parent_id"), table_name="message_comment")
    op.drop_index(op.f("ix_message_comment_message_id"), table_name="message_comment")
    op.drop_table("message_comment")
