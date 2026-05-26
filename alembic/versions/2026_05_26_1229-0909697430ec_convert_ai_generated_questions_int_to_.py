"""convert ai_generated_questions int to object

Revision ID: 0909697430ec
Revises: 84ab990a0092
Create Date: 2026-05-26 12:29:20.512603

"""

# NOTE: If this migration uses `op.batch_alter_table` against any of `project`,
# `projectlocalization`, `testsetup`, or `testrun` (the tables referenced by
# the touch-last_updated triggers), wrap the upgrade/downgrade bodies with
# `uninstall_triggers(op.get_bind())` before and
# `install_triggers(op.get_bind())` after. SQLite batch-alter renames the
# table, which breaks any trigger that references it by name. See
# `app/db/triggers.py` and revision 3d64d3a385a1 for an example.
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

import app.db.types  # noqa: F401


# revision identifiers, used by Alembic.
revision: str = "0909697430ec"
down_revision: Union[str, None] = "84ab990a0092"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Tables whose `interview_guide` JSON column holds question sections with an
# `ai_generated_questions` field.
_TABLES = ("projectlocalization", "interview")


def _iter_guides(conn):
    for table in _TABLES:
        rows = conn.execute(
            sa.text(
                f"SELECT id, interview_guide FROM {table} "
                "WHERE interview_guide IS NOT NULL"
            )
        ).fetchall()
        for row_id, guide_raw in rows:
            guide = json.loads(guide_raw) if isinstance(guide_raw, str) else guide_raw
            yield table, row_id, guide


def _save(conn, table, row_id, guide) -> None:
    conn.execute(
        sa.text(f"UPDATE {table} SET interview_guide = :guide WHERE id = :id"),
        {"guide": json.dumps(guide), "id": row_id},
    )


def upgrade() -> None:
    """Convert each section's `ai_generated_questions` from a plain int into a
    GeneratedQuestions object: {n: <int>, max_probes_n: null, max_probes_time: null}."""
    conn = op.get_bind()
    for table, row_id, guide in _iter_guides(conn):
        changed = False
        for section in guide.get("question_sections") or []:
            value = section.get("ai_generated_questions")
            if isinstance(value, int):
                section["ai_generated_questions"] = {
                    "n": value,
                    "max_probes_n": None,
                    "max_probes_time": None,
                }
                changed = True
        if changed:
            _save(conn, table, row_id, guide)


def downgrade() -> None:
    """Collapse the GeneratedQuestions object back into the plain int `n`."""
    conn = op.get_bind()
    for table, row_id, guide in _iter_guides(conn):
        changed = False
        for section in guide.get("question_sections") or []:
            value = section.get("ai_generated_questions")
            if isinstance(value, dict):
                section["ai_generated_questions"] = value.get("n", 0)
                changed = True
        if changed:
            _save(conn, table, row_id, guide)
