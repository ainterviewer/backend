"""Add one-time interview resume links

A resume link lets a respondent finish a specific interview in a browser that
has neither the ``interview_token`` cookie nor the localStorage entry the
frontend normally resumes from -- a new device, a cleared session, a phone
instead of a laptop.

The row stores only a sha256 hash of the token, like ``refresh_token``: the
plaintext is shown to the project member once, at creation. It is a bearer
credential for one interview's transcript, so it is bound to ``interview_id``
(never to a participant), single-redemption, expiring, and revocable.

Revision ID: d4f83a01c96b
Revises: b8d31f0a75c2
Create Date: 2026-08-26 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4f83a01c96b"
down_revision: str | None = "b8d31f0a75c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "interview_resume_token",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("interview_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("created_by", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("redeemed_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["interview_id"],
            ["interview.id"],
            name=op.f("fk_interview_resume_token_interview_id_interview"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["user.id"],
            name=op.f("fk_interview_resume_token_created_by_user"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_interview_resume_token")),
        sa.UniqueConstraint("id", name=op.f("uq_interview_resume_token_id")),
    )
    op.create_index(
        op.f("ix_interview_resume_token_interview_id"),
        "interview_resume_token",
        ["interview_id"],
    )
    # Unique as well as indexed: redemption looks the token up by hash alone,
    # and two rows sharing a hash would make that lookup ambiguous.
    op.create_index(
        op.f("ix_interview_resume_token_token_hash"),
        "interview_resume_token",
        ["token_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_interview_resume_token_token_hash"),
        table_name="interview_resume_token",
    )
    op.drop_index(
        op.f("ix_interview_resume_token_interview_id"),
        table_name="interview_resume_token",
    )
    op.drop_table("interview_resume_token")
