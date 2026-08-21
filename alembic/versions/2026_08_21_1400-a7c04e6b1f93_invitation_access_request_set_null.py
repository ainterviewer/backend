"""invitation.access_request_id on delete set null

An invitation outlives the access request that produced it: pruning the access
request list is list maintenance, not a way to revoke a pending invite. The
foreign key declared no `ondelete`, which meant three different answers to the
same question -- the ORM relationship deleted the invitation (`cascade="all,
delete-orphan"`), the database refused the parent delete once foreign keys were
enforced, and with them unenforced the reference was simply left dangling.

This makes the database agree with the intended behaviour. The relationship's
delete cascade is dropped in the same change, and
`UserRepository.delete_access_requests` clears the link explicitly so the
behaviour holds while foreign keys are still unenforced in production.

Revision ID: a7c04e6b1f93
Revises: e5f2a91c4d80
Create Date: 2026-08-21 14:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c04e6b1f93"
down_revision: str | None = "e5f2a91c4d80"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

_CONSTRAINT = "fk_invitation_access_request_id_access_requests"


def _rewrite(ondelete: str | None) -> None:
    with op.batch_alter_table(
        "invitation", naming_convention=NAMING_CONVENTION
    ) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT, type_="foreignkey")
        batch_op.create_foreign_key(
            _CONSTRAINT,
            "access_requests",
            ["access_request_id"],
            ["id"],
            ondelete=ondelete,
        )


def upgrade() -> None:
    _rewrite("SET NULL")


def downgrade() -> None:
    _rewrite(None)
