"""add password_reset verification purpose

Adds the PASSWORD_RESET member of the verificationpurpose enum, used by the
forgot-password magic link. SQLite stores the enum as a plain VARCHAR with no
CHECK constraint, so only PostgreSQL needs the type altered.

Revision ID: e2a7c1f09b3d
Revises: c7bde657f2c4
Create Date: 2026-10-05 12:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2a7c1f09b3d"
down_revision: str | None = "c7bde657f2c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "ALTER TYPE verificationpurpose ADD VALUE IF NOT EXISTS 'PASSWORD_RESET'"
        )


def downgrade() -> None:
    # The PASSWORD_RESET enum value is left in place on PostgreSQL: removing an
    # enum value requires rebuilding the type and any rows using it. Drop the
    # rows that use it so the remaining data matches the downgraded code.
    op.execute("DELETE FROM verification_code WHERE purpose = 'PASSWORD_RESET'")
