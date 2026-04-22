"""remove lang from agent config

Revision ID: 4d693d77718b
Revises: 11fe1cf630a3
Create Date: 2026-03-24 15:46:25.343127

"""

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4d693d77718b"
down_revision: Union[str, None] = "11fe1cf630a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT id, agent_configs FROM projectlocalization WHERE agent_configs IS NOT NULL"
        )
    ).fetchall()

    for row_id, agent_configs_raw in rows:
        if isinstance(agent_configs_raw, str):
            agent_configs = json.loads(agent_configs_raw)
        else:
            agent_configs = agent_configs_raw

        changed = False
        for key, config in agent_configs.items():
            if isinstance(config, dict) and "lang" in config:
                del config["lang"]
                changed = True

        if changed:
            conn.execute(
                sa.text(
                    "UPDATE projectlocalization SET agent_configs = :configs WHERE id = :id"
                ),
                {"configs": json.dumps(agent_configs), "id": row_id},
            )


def downgrade() -> None:
    # lang field cannot be restored — the values are lost
    pass
