"""clean stale survey items from interview guides

Nullifies survey_item entries inside interview_guide JSON that no longer
validate against the current SurveyItem schema.  Applies to both the
``interview`` and ``projectlocalization`` tables.

Revision ID: 11fe1cf630a3
Revises: d76ee7c3a020
Create Date: 2026-03-20 14:51:14.585969

"""

import json
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pydantic import TypeAdapter, ValidationError

from ainterviewer.interview_guides import SurveyItem

logger = logging.getLogger(__name__)

# revision identifiers, used by Alembic.
revision: str = "11fe1cf630a3"
down_revision: Union[str, None] = "d76ee7c3a020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_survey_item_adapter = TypeAdapter(SurveyItem)


def _clean_guide(guide: dict) -> bool:
    """Strip invalid survey_item entries from an interview_guide dict.

    Returns True if any modifications were made.
    """
    changed = False
    for section in guide.get("question_sections", []):
        for question in section.get("questions", []):
            item = question.get("survey_item")
            if item is None:
                continue
            try:
                _survey_item_adapter.validate_python(item)
            except ValidationError:
                question["survey_item"] = None
                changed = True
    return changed


def _clean_table(conn, table_name: str) -> None:
    rows = conn.execute(
        sa.text(
            f"SELECT id, interview_guide FROM {table_name} "
            f"WHERE interview_guide IS NOT NULL"
        )
    ).fetchall()

    cleaned = 0
    for row_id, guide_raw in rows:
        guide = guide_raw if isinstance(guide_raw, dict) else json.loads(guide_raw)

        if not isinstance(guide, dict):
            continue

        if _clean_guide(guide):
            conn.execute(
                sa.text(
                    f"UPDATE {table_name} SET interview_guide = :val WHERE id = :id"
                ),
                {"val": json.dumps(guide), "id": row_id},
            )
            cleaned += 1

    if cleaned:
        logger.info(
            "Cleaned invalid survey_items in %d rows of %s", cleaned, table_name
        )


def upgrade() -> None:
    conn = op.get_bind()
    _clean_table(conn, "interview")
    _clean_table(conn, "projectlocalization")


def downgrade() -> None:
    # Cannot restore nullified survey_item entries.
    pass
