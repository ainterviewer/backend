"""add audio_file to message

Revision ID: a9128ba6abfb
Revises: 0909697430ec
Create Date: 2026-06-10 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

import app.db.types  # noqa: F401


# revision identifiers, used by Alembic.
revision: str = "a9128ba6abfb"
down_revision: Union[str, None] = "0909697430ec"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add message.audio_file (filename of the recording a transcribed message
    came from) and, on PostgreSQL, the AUDIO member of the messagetype enum."""
    op.add_column("message", sa.Column("audio_file", sa.String(), nullable=True))

    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TYPE messagetype ADD VALUE IF NOT EXISTS 'AUDIO'")


def downgrade() -> None:
    # The AUDIO enum value is left in place on PostgreSQL: removing an enum
    # value requires rebuilding the type and any rows using it.
    op.drop_column("message", "audio_file")
