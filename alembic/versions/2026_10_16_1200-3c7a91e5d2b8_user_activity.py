"""user activity

`user_activity` holds one row per user per day on which they used the
dashboard, the history behind the admin overview's daily/weekly active users
and retention. `user.last_active` is a single overwritten stamp and cannot
answer "who was active on the 3rd".

The table is seeded with rows reconstructed from what users left
behind with a timestamp: signing up, logging in, owning a project, coding,
commenting, keeping a content version, starting an experiment or an
assistance session, and the day of `user.last_active`. That is a lower bound
-- a day spent only reading left nothing. Rows from here on are written live
by authenticated requests.

`user.last_login` becomes nullable, NULL until a first login: it used to be
stamped at sign-up, so an account that never logged in read as having logged
in the moment it was created. Existing rows whose `last_login` is within a
second of `created_at` are such stamps (a real login follows email
verification, seconds later at the least) and are cleared.

Revision ID: 3c7a91e5d2b8
Revises: da738218b8a4
Create Date: 2026-10-16 12:00:00

"""

# NOTE: Don't manage the touch-last_updated triggers here. alembic/env.py
# uninstalls them before every run and reinstalls the full set from
# app/db/triggers.py after, so batch_alter_table on `project`, `testrun` etc.
# cannot lose or break them.
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.types

# revision identifiers, used by Alembic.
revision: str = "3c7a91e5d2b8"
down_revision: str | None = "da738218b8a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, user column, timestamp column): every place a user's action left a
# timestamp. `user.created_at` / `last_login` / `last_active` are on the user
# row itself. `last_active` was bumped by token refreshes, so it can credit a
# day on which a tab was merely left open -- one day per user, at most.
_SOURCES = [
    ("user", "id", "created_at"),
    ("user", "id", "last_login"),
    ("user", "id", "last_active"),
    ("project", "owner_id", "created_at"),
    ("coding", "user_id", "created_at"),
    ("coding", "user_id", "updated_at"),
    ("message_comment", "user_id", "created_at"),
    ("projectcontentversion", "created_by_id", "created_at"),
    ("experiment", "user_id", "created_at"),
    ("assistance_session", "user_id", "created_at"),
]


def upgrade() -> None:
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.alter_column(
            "last_login", existing_type=app.db.types.AppDateTime(), nullable=True
        )
    op.execute(
        'UPDATE "user" SET last_login = NULL '
        "WHERE abs(julianday(last_login) - julianday(created_at)) * 86400 < 1"
    )

    op.create_table(
        "user_activity",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("first_seen", app.db.types.AppDateTime(), nullable=False),
        sa.Column("last_seen", app.db.types.AppDateTime(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_user_activity_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_activity")),
        sa.UniqueConstraint("id", name=op.f("uq_user_activity_id")),
        sa.UniqueConstraint("user_id", "day", name="user_day"),
    )
    with op.batch_alter_table("user_activity", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_user_activity_user_id"), ["user_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_user_activity_day"), ["day"], unique=False)

    # Timestamps are stored as naive app-time wall clock (AppDateTime), so
    # date() gives the app's calendar day, which is what `day` means live.
    # Uuid() on SQLite is 32 hex chars without dashes.
    events = " UNION ALL ".join(
        f'SELECT t."{user_col}" AS user_id, t."{ts_col}" AS ts '
        f'FROM "{table}" t WHERE t."{user_col}" IS NOT NULL '
        f'AND t."{ts_col}" IS NOT NULL'
        for table, user_col, ts_col in _SOURCES
    )
    op.execute(
        "INSERT INTO user_activity (id, user_id, day, first_seen, last_seen) "
        "SELECT lower(hex(randomblob(16))), e.user_id, date(e.ts), "
        "min(e.ts), max(e.ts) "
        f"FROM ({events}) e "
        # Only users that still exist: with foreign keys off, rows of deleted
        # users can linger in the source tables.
        'JOIN "user" u ON u.id = e.user_id '
        "GROUP BY e.user_id, date(e.ts)"
    )


def downgrade() -> None:
    with op.batch_alter_table("user_activity", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_user_activity_day"))
        batch_op.drop_index(batch_op.f("ix_user_activity_user_id"))
    op.drop_table("user_activity")

    op.execute('UPDATE "user" SET last_login = created_at WHERE last_login IS NULL')
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.alter_column(
            "last_login", existing_type=app.db.types.AppDateTime(), nullable=False
        )
