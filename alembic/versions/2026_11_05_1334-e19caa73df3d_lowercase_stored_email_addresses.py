"""lowercase stored email addresses

Revision ID: e19caa73df3d
Revises: 01de1ed43cd1
Create Date: 2026-11-05 13:34:56.031238

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
revision: str = "e19caa73df3d"
down_revision: str | None = "01de1ed43cd1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Tables whose `email` must be unique once lowercased. `collaboratorinvite`
# has always stored it lowercased; participant emails are respondents, not
# accounts, and are left as entered.
TABLES = ("user", "access_requests", "invitation", "newsletter_subscription")


def upgrade() -> None:
    """Store every account-side address lowercased (`normalize_email`).

    Refuses to run if two rows of a table differ only by case: those are two
    accounts (or requests) for one address, and which to keep is a decision,
    not something a migration should guess. Resolve them by hand -- merge or
    delete -- and run it again.
    """
    connection = op.get_bind()
    clashes = []
    for table in TABLES:
        rows = connection.execute(
            sa.text(
                f'SELECT lower(email), group_concat(email, ", ") FROM "{table}" '
                "WHERE email IS NOT NULL GROUP BY lower(email) HAVING count(*) > 1"
            )
        ).all()
        clashes += [f"{table}: {emails}" for _, emails in rows]
    if clashes:
        raise RuntimeError(
            "Addresses that differ only by case:\n  "
            + "\n  ".join(clashes)
            + "\nResolve these rows by hand, then run the migration again."
        )

    for table in TABLES:
        op.execute(
            f'UPDATE "{table}" SET email = lower(trim(email)) '
            "WHERE email IS NOT NULL AND email != lower(trim(email))"
        )


def downgrade() -> None:
    # The original casing is not kept, and nothing depends on it.
    pass
