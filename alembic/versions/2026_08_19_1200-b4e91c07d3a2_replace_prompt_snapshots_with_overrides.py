"""replace prompt snapshots with per-project overrides

Revision ID: b4e91c07d3a2
Revises: c7f21a5b4e10
Create Date: 2026-08-19 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

import app.db.types  # noqa: F401
from app.db.triggers import install_triggers, uninstall_triggers


# revision identifiers, used by Alembic.
revision: str = "b4e91c07d3a2"
down_revision: Union[str, None] = "c7f21a5b4e10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# `projectlocalization.prompts` held a full snapshot of the package's Jinja
# prompt templates, taken when the row was created. That snapshot was handed to
# a `DictLoader` as the interview's *only* template source, so a template added
# to the `ainterviewer` package after a project was created raised
# `TemplateNotFound` mid-interview, and an improved template never reached
# existing projects at all. Every such change needed its own data migration
# (see revision f7aaeeea0a76).
#
# Templates now resolve through the package at interview time, and this column
# holds only per-project overrides keyed by template name. Dropping the
# snapshot is lossless: nothing ever wrote user-authored text into it. The only
# writers were project creation, cloning, the blanket reset in revision
# f7aaeeea0a76 and the (now removed) `update_prompts` CLI command -- all of
# which stored the package defaults verbatim. Per-project prompt customisation
# is exposed through `agent_configs.probing.prompt_slots` instead.
#
# NOTE: this migration deliberately uses raw column operations rather than the
# ORM. Reading `projectlocalization` through the ORM would validate against the
# current `Prompts` model, which is exactly what breaks when the library adds a
# new agent -- the case this migration exists to fix.


def upgrade() -> None:
    bind = op.get_bind()
    # SQLite batch-alter recreates `projectlocalization`, which breaks the
    # touch-last_updated triggers that reference it by name.
    uninstall_triggers(bind)

    with op.batch_alter_table("projectlocalization", schema=None) as batch_op:
        batch_op.drop_column("prompts")
        batch_op.add_column(
            sa.Column(
                "prompt_overrides",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )

    install_triggers(bind)


def downgrade() -> None:
    bind = op.get_bind()
    uninstall_triggers(bind)

    # Restore the snapshot column and refill it with the current package
    # defaults. Any per-project overrides are dropped: the old column had no
    # place to put them.
    from ainterviewer.agents.prompts.models import DEFAULT_PROMPTS

    with op.batch_alter_table("projectlocalization", schema=None) as batch_op:
        batch_op.drop_column("prompt_overrides")
        batch_op.add_column(sa.Column("prompts", sa.JSON(), nullable=True))

    projectlocalization = sa.table(
        "projectlocalization",
        sa.column("prompts", sa.JSON()),
    )
    op.execute(
        projectlocalization.update().values(
            prompts=DEFAULT_PROMPTS.model_dump(mode="json")
        )
    )

    with op.batch_alter_table("projectlocalization", schema=None) as batch_op:
        batch_op.alter_column("prompts", nullable=False)

    install_triggers(bind)
