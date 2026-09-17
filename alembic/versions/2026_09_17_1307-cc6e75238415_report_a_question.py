"""let a respondent report an interviewer question

Adds `message_report`, the respondent's own channel for saying that a question
is inappropriate, offensive or irrelevant, and `message_report_read`, which
records that a reviewer has seen one.

Not a widening of `message.feedback`: that column is a thumbs rating, and "I
disliked this" is not the same claim as "this question is offensive". A report
carries a reason and, optionally, the respondent's own words.

A report holds *two* review states, one per reviewer -- `status` for the
project owner and `admin_status` for the platform admin, each with its own
resolver and timestamp. Separate rather than shared because neither queue may
clear the other's: an owner deciding to reword a question says nothing about
whether a platform admin has reviewed it for safety.

Both tables are new and start empty, so this is reversible as it stands.

Revision ID: cc6e75238415
Revises: c41b6d8ae207
Create Date: 2026-09-17 13:07:58.559595

"""

# NOTE: If this migration uses `op.batch_alter_table` against any of `project`,
# `projectlocalization`, `testsetup`, or `testrun` (the tables referenced by
# the touch-last_updated triggers), wrap the upgrade/downgrade bodies with
# `uninstall_triggers(op.get_bind())` before and
# `install_triggers(op.get_bind())` after. SQLite batch-alter renames the
# table, which breaks any trigger that references it by name. See
# `app/db/triggers.py` and revision 3d64d3a385a1 for an example.
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.types  # noqa: F401

# revision identifiers, used by Alembic.
revision: str = "cc6e75238415"
down_revision: str | None = "c41b6d8ae207"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "message_report",
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("interview_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        # `SQLEnum` stores the member's *name*, so these are the strings in
        # the column -- see `app.db.types.ReportReason`.
        sa.Column(
            "reason",
            sa.Enum(
                "INAPPROPRIATE", "OFFENSIVE", "IRRELEVANT", "OTHER", name="reportreason"
            ),
            nullable=False,
        ),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("OPEN", "RESOLVED", "DISMISSED", name="reportstatus"),
            nullable=False,
        ),
        sa.Column("resolved_by_id", sa.Uuid(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column(
            "admin_status",
            sa.Enum("OPEN", "RESOLVED", "DISMISSED", name="reportstatus"),
            nullable=False,
        ),
        sa.Column("admin_resolved_by_id", sa.Uuid(), nullable=True),
        sa.Column("admin_resolved_at", sa.DateTime(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["admin_resolved_by_id"],
            ["user.id"],
            name=op.f("fk_message_report_admin_resolved_by_id_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["interview_id"],
            ["interview.id"],
            name=op.f("fk_message_report_interview_id_interview"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["message.id"],
            name=op.f("fk_message_report_message_id_message"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_message_report_project_id_project"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by_id"],
            ["user.id"],
            name=op.f("fk_message_report_resolved_by_id_user"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message_report")),
        sa.UniqueConstraint("id", name=op.f("uq_message_report_id")),
    )
    with op.batch_alter_table("message_report", schema=None) as batch_op:
        batch_op.create_index(
            "ix_message_report_interview_id", ["interview_id"], unique=False
        )
        batch_op.create_index(
            "ix_message_report_message_id", ["message_id"], unique=False
        )
        batch_op.create_index(
            "ix_message_report_project_id_status_created_at",
            ["project_id", "status", "created_at"],
            unique=False,
        )

    op.create_table(
        "message_report_read",
        sa.Column("report_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("read_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["message_report.id"],
            name=op.f("fk_message_report_read_report_id_message_report"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_message_report_read_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message_report_read")),
        sa.UniqueConstraint("id", name=op.f("uq_message_report_read_id")),
        sa.UniqueConstraint("report_id", "user_id", name="uq_message_report_read"),
    )
    with op.batch_alter_table("message_report_read", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_message_report_read_report_id"), ["report_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_message_report_read_user_id"), ["user_id"], unique=False
        )


def downgrade() -> None:
    """Drop both tables, reads first.

    Child before parent: the app does not enable `PRAGMA foreign_keys` on
    SQLite, so dropping `message_report` first would leave the read rows
    behind on a database that does enforce them.
    """
    with op.batch_alter_table("message_report_read", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_message_report_read_user_id"))
        batch_op.drop_index(batch_op.f("ix_message_report_read_report_id"))

    op.drop_table("message_report_read")
    with op.batch_alter_table("message_report", schema=None) as batch_op:
        batch_op.drop_index("ix_message_report_project_id_status_created_at")
        batch_op.drop_index("ix_message_report_message_id")
        batch_op.drop_index("ix_message_report_interview_id")

    op.drop_table("message_report")
