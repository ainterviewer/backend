"""normalize_none_framing_introduction_in_interview_guides

Revision ID: 8804ba71199e
Revises: 0aeedbbaa569
Create Date: 2026-04-23 10:45:17.903978

"""

import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "8804ba71199e"
down_revision: Union[str, None] = "0aeedbbaa569"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLES = ("projectlocalization", "interview")


def _normalize(guide: dict) -> bool:
    changed = False
    for field in ("framing", "introduction"):
        if guide.get(field) is None:
            guide[field] = ""
            changed = True
    return changed


def upgrade() -> None:
    bind = op.get_bind()
    for table in TABLES:
        rows = bind.execute(
            sa.text(f"SELECT id, interview_guide FROM {table}")
        ).fetchall()
        for row_id, guide_raw in rows:
            if guide_raw is None:
                continue
            guide = json.loads(guide_raw) if isinstance(guide_raw, str) else guide_raw
            if not isinstance(guide, dict):
                continue
            if _normalize(guide):
                bind.execute(
                    sa.text(f"UPDATE {table} SET interview_guide = :g WHERE id = :id"),
                    {"g": json.dumps(guide), "id": row_id},
                )


def downgrade() -> None:
    pass
