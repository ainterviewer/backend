"""merge release/0.4.x hotfix migrations

Prod took three migrations as hotfixes on release/0.4.x (v0.4.37, v0.4.38),
branching off d4f83a01c96b alongside main's own later migrations:

    d4f83a01c96b -> c7bde657f2c4 -> e2a7c1f09b3d -> 1a0e5c9b7d42   (release/0.4.x)
    d4f83a01c96b -> ... -> c41b6d8ae207                            (main)

This joins the two lines, so a database at either head (prod at 1a0e5c9b7d42,
staging at c41b6d8ae207) upgrades by running only the other line's migrations.
The two lines touch different things, so there is nothing to reconcile.

Revision ID: d5f6e39dd77e
Revises: c41b6d8ae207, 1a0e5c9b7d42
Create Date: 2026-10-05 16:24:45.374606

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "d5f6e39dd77e"
down_revision: str | Sequence[str] | None = ("c41b6d8ae207", "1a0e5c9b7d42")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
