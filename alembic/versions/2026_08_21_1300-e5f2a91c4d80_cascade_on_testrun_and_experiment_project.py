"""cascade on testrun and experiment_project foreign keys

`TestRepository.delete_test_setup` and `delete_experiment` delete their parent
row with a Core `delete()` statement, which bypasses the ORM relationship
cascades and leaves the database to clean up the children. Their child foreign
keys declared no `ondelete`, so the database would refuse the delete once
foreign keys are enforced -- and, with enforcement off, silently orphaned the
children instead.

Adds ON DELETE CASCADE to:

* testrun.test_setup_id -> testsetup.id
* experiment_project.experiment_id -> experiment.id
* experiment_project.project_id -> project.id

This is inert until `foreign_keys=ON` reaches the connections (see
app/db/pragmas.py); the repositories delete their children explicitly so they
are correct either way.

Note for future SQLite migrations: `batch_alter_table` rebuilds the table by
copy-and-rename, which must not run with foreign keys enforced. Alembic builds
its own engine in alembic/env.py and so never picks up the app's pragmas --
keep it that way.

Revision ID: e5f2a91c4d80
Revises: c3d18b40f2a7
Create Date: 2026-08-21 13:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5f2a91c4d80"
down_revision: str | None = "c3d18b40f2a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copy of the metadata's convention, so this migration keeps producing
# the same constraint names even if the application's convention changes.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# (table, constraint name, column, referred table)
_FOREIGN_KEYS: tuple[tuple[str, str, str, str], ...] = (
    ("testrun", "fk_testrun_test_setup_id_testsetup", "test_setup_id", "testsetup"),
    (
        "experiment_project",
        "fk_experiment_project_experiment_id_experiment",
        "experiment_id",
        "experiment",
    ),
    (
        "experiment_project",
        "fk_experiment_project_project_id_project",
        "project_id",
        "project",
    ),
)


def _rewrite(ondelete: str | None) -> None:
    for table, name, column, referred in _FOREIGN_KEYS:
        with op.batch_alter_table(
            table, naming_convention=NAMING_CONVENTION
        ) as batch_op:
            batch_op.drop_constraint(name, type_="foreignkey")
            batch_op.create_foreign_key(
                name, referred, [column], ["id"], ondelete=ondelete
            )


def upgrade() -> None:
    _rewrite("CASCADE")


def downgrade() -> None:
    _rewrite(None)
