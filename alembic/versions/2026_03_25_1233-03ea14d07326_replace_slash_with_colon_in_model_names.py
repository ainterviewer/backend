"""replace slash with colon in model names

Revision ID: 03ea14d07326
Revises: 4d693d77718b
Create Date: 2026-03-25 12:33:03.357113

"""

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

AGENT_CONFIG_KEYS = [
    "probing",
    "classification",
    "history",
    "security",
    "visual",
    "answering",
]

# revision identifiers, used by Alembic.
revision: str = "03ea14d07326"
down_revision: Union[str, None] = "4d693d77718b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _replace_first_slash(value: str) -> str:
    return value.replace("/", ":", 1)


def _replace_first_colon(value: str) -> str:
    return value.replace(":", "/", 1)


def upgrade() -> None:
    conn = op.get_bind()

    # --- projectlocalization.agent_configs ---
    rows = conn.execute(
        sa.text(
            "SELECT id, agent_configs FROM projectlocalization WHERE agent_configs IS NOT NULL"
        )
    ).fetchall()

    for row_id, agent_configs_raw in rows:
        agent_configs = (
            json.loads(agent_configs_raw)
            if isinstance(agent_configs_raw, str)
            else agent_configs_raw
        )

        changed = False
        for key in AGENT_CONFIG_KEYS:
            config = agent_configs.get(key)
            if (
                isinstance(config, dict)
                and "model" in config
                and "/" in config["model"]
                and ":" not in config["model"]
            ):
                config["model"] = _replace_first_slash(config["model"])
                changed = True

        if changed:
            conn.execute(
                sa.text(
                    "UPDATE projectlocalization SET agent_configs = :configs WHERE id = :id"
                ),
                {"configs": json.dumps(agent_configs), "id": row_id},
            )

    # --- testsetup.answering_model ---
    conn.execute(
        sa.text(
            "UPDATE testsetup SET answering_model = SUBSTR(answering_model, 1, INSTR(answering_model, '/') - 1) "
            "|| ':' || SUBSTR(answering_model, INSTR(answering_model, '/') + 1) "
            "WHERE answering_model LIKE '%/%' AND answering_model NOT LIKE '%:%'"
        )
    )

    # --- testrun.answering_model ---
    conn.execute(
        sa.text(
            "UPDATE testrun SET answering_model = SUBSTR(answering_model, 1, INSTR(answering_model, '/') - 1) "
            "|| ':' || SUBSTR(answering_model, INSTR(answering_model, '/') + 1) "
            "WHERE answering_model LIKE '%/%' AND answering_model NOT LIKE '%:%'"
        )
    )


def downgrade() -> None:
    conn = op.get_bind()

    # --- projectlocalization.agent_configs ---
    rows = conn.execute(
        sa.text(
            "SELECT id, agent_configs FROM projectlocalization WHERE agent_configs IS NOT NULL"
        )
    ).fetchall()

    for row_id, agent_configs_raw in rows:
        agent_configs = (
            json.loads(agent_configs_raw)
            if isinstance(agent_configs_raw, str)
            else agent_configs_raw
        )

        changed = False
        for key in AGENT_CONFIG_KEYS:
            config = agent_configs.get(key)
            if (
                isinstance(config, dict)
                and "model" in config
                and ":" in config["model"]
            ):
                config["model"] = _replace_first_colon(config["model"])
                changed = True

        if changed:
            conn.execute(
                sa.text(
                    "UPDATE projectlocalization SET agent_configs = :configs WHERE id = :id"
                ),
                {"configs": json.dumps(agent_configs), "id": row_id},
            )

    # --- testsetup.answering_model ---
    conn.execute(
        sa.text(
            "UPDATE testsetup SET answering_model = SUBSTR(answering_model, 1, INSTR(answering_model, ':') - 1) "
            "|| '/' || SUBSTR(answering_model, INSTR(answering_model, ':') + 1) "
            "WHERE answering_model LIKE '%:%'"
        )
    )

    # --- testrun.answering_model ---
    conn.execute(
        sa.text(
            "UPDATE testrun SET answering_model = SUBSTR(answering_model, 1, INSTR(answering_model, ':') - 1) "
            "|| '/' || SUBSTR(answering_model, INSTR(answering_model, ':') + 1) "
            "WHERE answering_model LIKE '%:%'"
        )
    )
