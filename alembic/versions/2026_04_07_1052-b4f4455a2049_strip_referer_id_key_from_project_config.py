"""strip referer_id_key from project config

Revision ID: b4f4455a2049
Revises: 3959c384af31
Create Date: 2026-04-07 10:52:58.878970

"""

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4f4455a2049"
down_revision: Union[str, None] = "3959c384af31"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, config FROM project WHERE config IS NOT NULL")
    ).fetchall()

    for row_id, config_raw in rows:
        if isinstance(config_raw, str):
            config = json.loads(config_raw)
        else:
            config = config_raw

        if "referer_id_key" in config:
            del config["referer_id_key"]
            conn.execute(
                sa.text("UPDATE project SET config = :config WHERE id = :id"),
                {"config": json.dumps(config), "id": row_id},
            )


def downgrade() -> None:
    # referer_id_key was removed from InterviewConfig — cannot be restored
    pass
