"""add guide_agent to prompts in projectlocalization

Revision ID: 7a550051f39a
Revises: b4f4455a2049
Create Date: 2026-04-07 11:13:45.148587

"""

import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "7a550051f39a"
down_revision: Union[str, None] = "b4f4455a2049"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from ainterviewer.agents.prompts.models import DEFAULT_PROMPTS

    default_guide_agent = DEFAULT_PROMPTS.guide_agent.model_dump()

    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, prompts FROM projectlocalization WHERE prompts IS NOT NULL")
    ).fetchall()

    for row_id, prompts_raw in rows:
        if isinstance(prompts_raw, str):
            prompts = json.loads(prompts_raw)
        else:
            prompts = prompts_raw

        if "guide_agent" not in prompts:
            prompts["guide_agent"] = default_guide_agent
            conn.execute(
                sa.text(
                    "UPDATE projectlocalization SET prompts = :prompts WHERE id = :id"
                ),
                {"prompts": json.dumps(prompts), "id": row_id},
            )


def downgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, prompts FROM projectlocalization WHERE prompts IS NOT NULL")
    ).fetchall()

    for row_id, prompts_raw in rows:
        if isinstance(prompts_raw, str):
            prompts = json.loads(prompts_raw)
        else:
            prompts = prompts_raw

        if "guide_agent" in prompts:
            del prompts["guide_agent"]
            conn.execute(
                sa.text(
                    "UPDATE projectlocalization SET prompts = :prompts WHERE id = :id"
                ),
                {"prompts": json.dumps(prompts), "id": row_id},
            )
