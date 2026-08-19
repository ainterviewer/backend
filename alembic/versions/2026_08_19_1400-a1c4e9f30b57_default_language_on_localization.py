"""move default_language from project config onto the localization row

Revision ID: a1c4e9f30b57
Revises: b4e91c07d3a2
Create Date: 2026-08-19 14:00:00.000000

"""

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

import app.db.types  # noqa: F401
from app.db.triggers import install_triggers, uninstall_triggers


# revision identifiers, used by Alembic.
revision: str = "a1c4e9f30b57"
down_revision: Union[str, None] = "b4e91c07d3a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# `InterviewConfig.default_language` was a bare string inside the project's
# JSON config with nothing tying it to the `projectlocalization` rows it named.
# Deleting the localization it pointed at left it dangling, which made
# `_get_localization` raise and broke interview creation, the translation
# source for new languages, and the dashboard's per-language routes.
#
# The flag now lives on the localization itself, with a partial unique index
# holding "exactly one default per project". Because `InterviewConfig` is
# declared with `extra="forbid"`, the key MUST be stripped from every stored
# config in the same revision that removes the field from the model -- an
# untouched config would fail to deserialize.

project = sa.table("project", sa.column("id"), sa.column("config", sa.JSON()))
localization = sa.table(
    "projectlocalization",
    sa.column("id"),
    sa.column("project_id"),
    sa.column("language"),
    sa.column("is_default", sa.Boolean()),
    sa.column("created_at"),
)


def _as_dict(config) -> dict:
    """Configs come back as dicts on Postgres JSONB and as text on SQLite."""
    if isinstance(config, str):
        return json.loads(config)
    return dict(config or {})


def upgrade() -> None:
    bind = op.get_bind()
    # SQLite batch-alter recreates `projectlocalization`, which breaks the
    # touch-last_updated triggers that reference it by name. Data backfill
    # also runs with triggers off so it doesn't bump every project's
    # last_updated.
    uninstall_triggers(bind)

    with op.batch_alter_table("projectlocalization", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_default",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )

    op.create_index(
        "uq_project_default_language",
        "projectlocalization",
        ["project_id"],
        unique=True,
        sqlite_where=sa.text("is_default"),
        postgresql_where=sa.text("is_default"),
    )

    configs = bind.execute(sa.select(project.c.id, project.c.config)).all()

    dangling = 0
    for project_id, raw_config in configs:
        config = _as_dict(raw_config)
        default_language = config.pop("default_language", None)

        rows = bind.execute(
            sa.select(localization.c.id, localization.c.language)
            .where(localization.c.project_id == project_id)
            .order_by(localization.c.created_at)
        ).all()

        if rows:
            match = next(
                (row for row in rows if row.language == default_language), None
            )
            if match is None:
                # The config named a language with no localization -- the exact
                # breakage this migration exists to prevent. Fall back to the
                # oldest localization, which is the one project creation seeded.
                dangling += 1
                match = rows[0]

            bind.execute(
                localization.update()
                .where(localization.c.id == match.id)
                .values(is_default=True)
            )

        bind.execute(
            project.update().where(project.c.id == project_id).values(config=config)
        )

    if dangling:
        print(
            f"[{revision}] {dangling} project(s) had a default_language with no "
            "matching localization; defaulted to their oldest localization."
        )

    install_triggers(bind)


def downgrade() -> None:
    bind = op.get_bind()
    uninstall_triggers(bind)

    rows = bind.execute(
        sa.select(localization.c.project_id, localization.c.language).where(
            localization.c.is_default
        )
    ).all()
    defaults = {row.project_id: row.language for row in rows}

    configs = bind.execute(sa.select(project.c.id, project.c.config)).all()
    for project_id, raw_config in configs:
        config = _as_dict(raw_config)
        config["default_language"] = defaults.get(project_id, "EN")
        bind.execute(
            project.update().where(project.c.id == project_id).values(config=config)
        )

    op.drop_index("uq_project_default_language", table_name="projectlocalization")

    with op.batch_alter_table("projectlocalization", schema=None) as batch_op:
        batch_op.drop_column("is_default")

    install_triggers(bind)
