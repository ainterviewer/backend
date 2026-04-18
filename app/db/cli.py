import json

import typer
from sqlalchemy import text
from sqlalchemy.orm import Session
from typer import Typer

from ainterviewer.agents.config import AgentConfigs
from ainterviewer.settings import settings as lib_settings

from ..dependencies import engine, get_db
from ..platform_release import PlatformManifest

cli = Typer()


@cli.command(hidden=True)
def _(): ...


@cli.command()
def add_release_manifest(release_manifest: str):
    db = next(get_db())

    platform_release_manifest = PlatformManifest.model_validate_json(release_manifest)

    db.set_platform_release(platform_manifest=platform_release_manifest)


def _as_dict(value):
    if value is None:
        return None
    if isinstance(value, str):
        return json.loads(value)
    return value


@cli.command()
def fix_invalid_models(dry_run: bool = False):
    """Replace any LLM model references not in lib_settings.llm.available_models
    with lib_settings.llm.default_model across all DB tables that store a model.

    Uses raw SQL to avoid ORM-level pydantic validation on unrelated JSON columns
    that may have drifted from the current schema.
    """

    available = lib_settings.llm.available_models
    default = lib_settings.llm.default_model

    prefix = "[DRY-RUN] " if dry_run else ""
    typer.echo(f"{prefix}Available models: {sorted(available)}")
    typer.echo(f"{prefix}Default model: {default}")

    changes = 0
    agent_fields = list(AgentConfigs.model_fields)

    with Session(engine) as session:
        # ProjectLocalizationTable.agent_configs (JSON)
        rows = session.execute(
            text("SELECT id, agent_configs FROM projectlocalization")
        ).all()
        for row_id, raw in rows:
            data = _as_dict(raw)
            if not isinstance(data, dict):
                continue
            mutated = False
            for field_name in agent_fields:
                sub = data.get(field_name)
                if isinstance(sub, dict) and sub.get("model") not in available:
                    typer.echo(
                        f"ProjectLocalization {row_id} agent_configs.{field_name}.model: "
                        f"{sub.get('model')!r} -> {default!r}"
                    )
                    sub["model"] = default
                    mutated = True
                    changes += 1
            if mutated:
                session.execute(
                    text(
                        "UPDATE projectlocalization SET agent_configs = :v WHERE id = :id"
                    ),
                    {"v": json.dumps(data), "id": row_id},
                )

        # TestSetupTable.answering_model
        rows = session.execute(text("SELECT id, answering_model FROM testsetup")).all()
        for row_id, model in rows:
            if model is not None and model not in available:
                typer.echo(
                    f"TestSetup {row_id} answering_model: {model!r} -> {default!r}"
                )
                session.execute(
                    text("UPDATE testsetup SET answering_model = :v WHERE id = :id"),
                    {"v": default, "id": row_id},
                )
                changes += 1

        # TestRunTable.answering_model
        rows = session.execute(text("SELECT id, answering_model FROM testrun")).all()
        for row_id, model in rows:
            if model is not None and model not in available:
                typer.echo(
                    f"TestRun {row_id} answering_model: {model!r} -> {default!r}"
                )
                session.execute(
                    text("UPDATE testrun SET answering_model = :v WHERE id = :id"),
                    {"v": default, "id": row_id},
                )
                changes += 1

        # TaskTable.model
        rows = session.execute(text("SELECT id, model FROM task")).all()
        for row_id, model in rows:
            if model is not None and model not in available:
                typer.echo(f"Task {row_id} model: {model!r} -> {default!r}")
                session.execute(
                    text("UPDATE task SET model = :v WHERE id = :id"),
                    {"v": default, "id": row_id},
                )
                changes += 1

        if dry_run:
            session.rollback()
            typer.echo(f"[DRY-RUN] Would update {changes} model reference(s).")
        else:
            session.commit()
            typer.echo(f"Updated {changes} model reference(s).")


if __name__ == "__main__":
    cli()
