"""disable_audio_for_all_projects

Revision ID: b31c7a4d9e02
Revises: f7aaeeea0a76
Create Date: 2026-08-12 12:00:00.000000

One-off data migration turning off voice answers (InterviewConfig.with_audio)
for every existing project. Irreversible: the previous per-project value is not
recorded, so downgrade is a no-op.

"""

import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b31c7a4d9e02"
down_revision: Union[str, None] = "f7aaeeea0a76"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, config FROM project")).fetchall()
    for row_id, config_raw in rows:
        if config_raw is None:
            continue
        config = json.loads(config_raw) if isinstance(config_raw, str) else config_raw
        if not isinstance(config, dict) or config.get("with_audio") is False:
            continue
        config["with_audio"] = False
        bind.execute(
            sa.text("UPDATE project SET config = :c WHERE id = :id"),
            {"c": json.dumps(config), "id": row_id},
        )


def downgrade() -> None:
    pass
