"""invitation alias

A nullable, unique `alias` on `invitation`: a readable token a sign-up link can
carry instead of the invitation's UUID (`/sign-up?token=workshop`).

Aliases replace `app.special_registration_tokens`, which were matched against
config and never stored. So that links already handed out keep working, each
token configured in `config.toml` becomes a reusable invitation with that
alias, the token's scope and no expiry. Alembic does not read the app
settings, so this reads the file itself -- the same `config.toml` in the
working directory that the app reads. A token set only through the
environment is not seen; recreate it from the admin page.

Revision ID: 8c9dc147b75f
Revises: a8c3e5f1d742
Create Date: 2026-10-22 12:00:00.000000

"""

# NOTE: Don't manage the touch-last_updated triggers here. alembic/env.py
# uninstalls them before every run and reinstalls the full set from
# app/db/triggers.py after, so batch_alter_table on `project`, `testrun` etc.
# cannot lose or break them.
import tomllib
import uuid
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

import app.db.types
from ainterviewer.utils import now
from app.schemas.users import normalize_invitation_alias
from app.types import Scope

# revision identifiers, used by Alembic.
revision: str = "8c9dc147b75f"
down_revision: str | None = "a8c3e5f1d742"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


invitation = sa.table(
    "invitation",
    sa.column("id", sa.Uuid()),
    sa.column("created_at", app.db.types.AppDateTime()),
    sa.column("reuseable", sa.Boolean()),
    sa.column("user_scope", sa.Enum(Scope)),
    sa.column("user_expires", sa.JSON()),
    sa.column("title", sa.String()),
    sa.column("alias", sa.String()),
)


def _configured_tokens() -> list[dict]:
    path = Path("config.toml")
    if not path.exists():
        return []
    config = tomllib.loads(path.read_text())
    return config.get("app", {}).get("special_registration_tokens", [])


def upgrade() -> None:
    with op.batch_alter_table("invitation", schema=None) as batch_op:
        batch_op.add_column(sa.Column("alias", sa.String(), nullable=True))
        batch_op.create_unique_constraint(op.f("uq_invitation_alias"), ["alias"])

    connection = op.get_bind()
    for token in _configured_tokens():
        alias = normalize_invitation_alias(token["token"])
        exists = connection.execute(
            sa.select(invitation.c.id).where(invitation.c.alias == alias)
        ).first()
        if exists:
            continue
        connection.execute(
            invitation.insert().values(
                id=uuid.uuid4(),
                created_at=now(),
                reuseable=True,
                user_scope=Scope(token["scope"]),
                user_expires=None,
                title=token["token"],
                alias=alias,
            )
        )


def downgrade() -> None:
    # The invitations made from config tokens stay: without their alias they
    # are unreachable, and the config tokens work again once restored.
    with op.batch_alter_table("invitation", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("uq_invitation_alias"), type_="unique")
        batch_op.drop_column("alias")
