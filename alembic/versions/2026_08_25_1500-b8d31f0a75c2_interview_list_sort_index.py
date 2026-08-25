"""Carry created_at in the interview project/type index

The interview and test-result lists page and sort in the database. Both filter
by project and type, then order by created_at, and the existing index stopped
at (project_id, type) -- so every page request sorted the project's whole
interview set in a temporary B-tree just to return twenty rows.

Measured on SQLite with 10k interviews in one project, the default view went
from 62ms to 4.5ms, and a whole request including the facet counts from 72ms to
19ms. The old index is dropped rather than kept: it is a strict prefix of this
one, so every query that used it can use this instead, and keeping both would
only add write cost.

Revision ID: b8d31f0a75c2
Revises: a7c04e6b1f93
Create Date: 2026-08-25 15:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8d31f0a75c2"
down_revision: str | None = "a7c04e6b1f93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "ix_interview_project_id_type"
_NEW = "ix_interview_project_id_type_created_at"


def upgrade() -> None:
    op.create_index(_NEW, "interview", ["project_id", "type", "created_at"])
    op.drop_index(_OLD, table_name="interview")


def downgrade() -> None:
    op.create_index(_OLD, "interview", ["project_id", "type"])
    op.drop_index(_NEW, table_name="interview")
