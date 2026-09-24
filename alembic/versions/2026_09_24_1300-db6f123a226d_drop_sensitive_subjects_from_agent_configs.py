"""drop sensitive_subjects from stored agent configs

The security agent no longer takes a list of sensitive subjects: what it checks
for is now a policy of decisions, `SecurityConfig.policy`, which defaults to
the library's own when a stored config has none. `SecurityConfig` forbids
unknown fields, so every agent config written before that change -- all of
which carry `security.sensitive_subjects`, usually as null -- failed to load
with `extra_forbidden`.

Agent configs live on the `ContentColumns` mixin, so they are stored twice
over: in the draft (`projectcontent`) and in every version
(`projectcontentversion`). Both are swept. A version is otherwise immutable,
but one that fails to load cannot be published, restored or interviewed on,
and dropping a key that no longer means anything changes nothing it records.
A test snapshot's stored `content_hash` no longer matches its columns after
this, so the next test interview on that content takes a fresh snapshot
rather than reusing it; nothing else reads the stored hash.

The key is removed rather than translated. A list of subjects has no
counterpart in a policy, and the old agent it configured is gone; a project
that had the agent switched on gets the default policy instead. Any list that
was actually set is logged, with its row, before it is dropped.

Irreversible in the sense that the lists cannot be brought back, but the
downgrade needs nothing: the old `SecurityConfig` defaulted the field to None.

Revision ID: db6f123a226d
Revises: 7f77bc35ec78
Create Date: 2026-09-24 13:00:00.000000

"""

import json
import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "db6f123a226d"
down_revision: str | None = "7f77bc35ec78"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

TABLES = tuple(
    sa.table(
        name,
        sa.column("id"),
        sa.column("project_id"),
        sa.column("agent_configs", sa.JSON()),
    )
    for name in ("projectcontent", "projectcontentversion")
)


def _sweep(table: sa.TableClause) -> int:
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(table.c.id, table.c.project_id, table.c.agent_configs)
    ).all()

    updated = 0
    for row in rows:
        agent_configs = row.agent_configs
        if isinstance(agent_configs, str):
            agent_configs = json.loads(agent_configs)

        security = (agent_configs or {}).get("security")
        if not isinstance(security, dict) or "sensitive_subjects" not in security:
            continue

        if subjects := security.pop("sensitive_subjects"):
            logger.warning(
                "Dropping sensitive_subjects %r from project %s (%s %s)",
                subjects,
                row.project_id,
                table.name,
                row.id,
            )

        bind.execute(
            sa.update(table)
            .where(table.c.id == row.id)
            .values(agent_configs=agent_configs)
        )
        updated += 1
    return updated


def upgrade() -> None:
    for table in TABLES:
        updated = _sweep(table)
        logger.info("Dropped sensitive_subjects from %d %s row(s)", updated, table.name)


def downgrade() -> None:
    pass
