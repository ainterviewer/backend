"""strip stale ui field from checkbox survey items

Revision ID: 43df73dcae59
Revises: 073a22823ffa
Create Date: 2026-03-20 14:44:18.648980

"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

import app.db.types


# revision identifiers, used by Alembic.
revision: str = '43df73dcae59'
down_revision: Union[str, None] = '073a22823ffa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    rows = conn.execute(
        sa.text(
            "SELECT id, survey_item FROM message "
            "WHERE survey_item IS NOT NULL"
        )
    ).fetchall()

    for row_id, survey_item_raw in rows:
        data = survey_item_raw if isinstance(survey_item_raw, dict) else json.loads(survey_item_raw)

        if not isinstance(data, dict):
            continue

        if data.get("type") == "checkbox" and "ui" in data:
            del data["ui"]
            conn.execute(
                sa.text("UPDATE message SET survey_item = :val WHERE id = :id"),
                {"val": json.dumps(data), "id": row_id},
            )


def downgrade() -> None:
    # The 'ui' field was stale data — no meaningful downgrade.
    pass
