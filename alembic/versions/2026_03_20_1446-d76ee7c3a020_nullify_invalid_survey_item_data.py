"""nullify invalid survey_item data

Revision ID: d76ee7c3a020
Revises: 43df73dcae59
Create Date: 2026-03-20 14:46:59.862397

"""
import json
import logging
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pydantic import TypeAdapter, ValidationError

from ainterviewer.interview_guides import SurveyItem

logger = logging.getLogger(__name__)

# revision identifiers, used by Alembic.
revision: str = 'd76ee7c3a020'
down_revision: Union[str, None] = '43df73dcae59'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    adapter = TypeAdapter(SurveyItem)

    rows = conn.execute(
        sa.text("SELECT id, survey_item FROM message WHERE survey_item IS NOT NULL")
    ).fetchall()

    nullified = 0
    for row_id, survey_item_raw in rows:
        data = survey_item_raw if isinstance(survey_item_raw, dict) else json.loads(survey_item_raw)

        if not isinstance(data, dict):
            continue

        try:
            adapter.validate_python(data)
        except ValidationError:
            conn.execute(
                sa.text("UPDATE message SET survey_item = NULL WHERE id = :id"),
                {"id": row_id},
            )
            nullified += 1

    if nullified:
        logger.info("Nullified %d invalid survey_item rows", nullified)


def downgrade() -> None:
    # Cannot restore nullified data.
    pass
