import json
from pathlib import Path
from uuid import UUID

import typer
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session
from typer import Typer

from ainterviewer.agents.config import AgentConfigs
from ainterviewer.settings import settings as lib_settings
from ainterviewer.types import DatabaseType

from ..dependencies import engine, get_db
from ..distribution import runs_on_answers, speech_is_test_only
from ..platform_release import Highlight, PlatformManifest
from ..schemas.users import UserCreate
from ..settings import app_settings
from .repositories.errors import EmailTakenError
from .seed import reset_and_seed

cli = Typer()


@cli.command(hidden=True)
def _(): ...


@cli.command()
def setup_db(
    fresh: bool = typer.Option(
        False, "--fresh", help="Delete existing database before setup"
    ),
    users_file: Path | None = typer.Option(
        None,
        "--users-file",
        help="Path to a users JSON file to seed when the database is created",
    ),
):
    """Create the database on first run; leave an existing one untouched.

    The migrations start from an existing schema, so a new database is built
    from the models and stamped at head instead. An existing database is left
    for `alembic upgrade head`: stamping it would mark migrations as applied
    that never ran. Safe to run on every start, which is what entrypoint.sh does.
    """
    if app_settings.database.db == DatabaseType.SQLITE:
        db_dir = Path(app_settings.database.db_path)
        db_dir.mkdir(parents=True, exist_ok=True)

        if fresh:
            db_file = db_dir / app_settings.database.database_file  # ty:ignore[unsupported-operator]
            if db_file.exists():
                typer.confirm(f"Delete {db_file} and all its data?", abort=True)
                db_file.unlink()
                typer.echo(f"Deleted {db_file}.")

    # Not "any table": the vector extension adds its own on first connect, so
    # a brand-new database is never empty by the time we look.
    if inspect(engine).has_table("user"):
        typer.echo("Database already exists, leaving it to migrations.")
        return

    db = next(get_db())
    db.create_db_and_tables()
    typer.echo("Database and tables created.")

    if users_file is None:
        return

    with users_file.open() as f:
        users = json.load(f)
    n = 0
    for user in users:
        try:
            db.users.create_user(UserCreate(**user))
            n += 1
        except EmailTakenError:
            typer.echo(f"User {user['email']} already exists, skipping.")
    typer.echo(f"Created {n} user{'s' if n != 1 else ''}.")


@cli.command()
def seed_dev(
    yes: bool = typer.Option(
        False, "--yes", help="Replace the database without prompting"
    ),
):
    """Replace the local SQLite database with realistic development fixtures."""
    reset_and_seed(yes=yes)


@cli.command()
def add_release_manifest(release_manifest: str):
    db = next(get_db())

    platform_release_manifest = PlatformManifest.model_validate_json(release_manifest)

    db.set_platform_release(platform_manifest=platform_release_manifest)


@cli.command()
def set_release_highlights(platform_version: str, highlights: str):
    """Attach curated user-facing highlights to an already-inserted release.

    `highlights` is a JSON array of Highlight objects, produced by the
    curation step in the deploy repo (`just release-notes`).
    """
    db = next(get_db())

    parsed = [Highlight.model_validate(item) for item in json.loads(highlights)]
    db.set_release_highlights(platform_version=platform_version, highlights=parsed)

    typer.echo(f"Set {len(parsed)} highlight(s) on {platform_version}")


def _as_dict(value):
    if value is None:
        return None
    if isinstance(value, str):
        return json.loads(value)
    return value


def invalid_model_changes(session: Session, to: str) -> list[str]:
    """Point every configured model that is not available at `to`, and
    describe each change. Writes in `session`; the caller commits or rolls
    back.

    Only where a model is *configured* -- the agents of the draft and of every
    version (respondents run the published one), and test setups -- never
    where one is *recorded*: `testrun.answering_model` and `task.model` say
    what ran at the time.

    An agent's own default counts as valid even when it is not in
    `available_models`: the visual agent defaults to `llava`, off and hidden in
    the UI. Raw SQL, so unrelated JSON that has drifted from the current schema
    does not have to validate.
    """
    available = set(lib_settings.llm.available_models)
    defaults = AgentConfigs()
    valid = {
        name: available | {getattr(defaults, name).model}
        for name in AgentConfigs.model_fields
    }
    changes: list[str] = []

    for table in ("projectcontent", "projectcontentversion"):
        rows = session.execute(text(f"SELECT id, agent_configs FROM {table}")).all()
        for row_id, raw in rows:
            data = _as_dict(raw)
            if not isinstance(data, dict):
                continue
            mutated = False
            for name, allowed in valid.items():
                sub = data.get(name)
                if isinstance(sub, dict) and sub.get("model") not in allowed:
                    changes.append(
                        f"{table} {row_id} agent_configs.{name}.model: "
                        f"{sub.get('model')!r} -> {to!r}"
                    )
                    sub["model"] = to
                    mutated = True
            if mutated:
                # The draft's revisions as well, as an ORM write would bump
                # them, so an editor that loaded the old model gets a conflict
                # rather than saving it back.
                bump = (
                    ", revision = revision + 1, agents_revision = agents_revision + 1"
                    if table == "projectcontent"
                    else ""
                )
                session.execute(
                    text(f"UPDATE {table} SET agent_configs = :v{bump} WHERE id = :id"),
                    {"v": json.dumps(data), "id": row_id},
                )

    rows = session.execute(text("SELECT id, answering_model FROM testsetup")).all()
    for row_id, model in rows:
        if model is not None and model not in available:
            changes.append(f"testsetup {row_id} answering_model: {model!r} -> {to!r}")
            session.execute(
                text("UPDATE testsetup SET answering_model = :v WHERE id = :id"),
                {"v": to, "id": row_id},
            )

    return changes


@cli.command()
def fix_invalid_models(
    to: str = typer.Option(
        ...,
        "--to",
        help="The available model to configure instead. Required, not the "
        "default model: choosing one chooses a provider, and so where "
        "respondents' answers are sent.",
    ),
    dry_run: bool = False,
):
    """Replace configured models that are not in `available_models` with `--to`.

    See `invalid_model_changes` for what counts and what is left alone. For a
    model that was only renamed, prefer a migration with an explicit mapping
    (revision 6b9e3953fd38): this replaces every unknown model with one.
    """
    available = lib_settings.llm.available_models
    if to not in available:
        typer.echo(f"{to!r} is not one of the available models: {sorted(available)}")
        raise typer.Exit(1)

    prefix = "[DRY-RUN] " if dry_run else ""
    with Session(engine) as session:
        changes = invalid_model_changes(session, to)
        for change in changes:
            typer.echo(f"{prefix}{change}")
        if dry_run:
            session.rollback()
            typer.echo(f"[DRY-RUN] Would update {len(changes)} model reference(s).")
        else:
            session.commit()
            typer.echo(f"Updated {len(changes)} model reference(s).")


def distributed_model_changes(
    session: Session, to: str, disable_audio: bool = False
) -> list[str]:
    """Open every published project to respondents again where a model is
    what closes it (app/distribution.py): point each agent that runs on
    respondents' answers and uses a test-only model at `to`, in the published
    version, and describe each change. Writes in `session`; the caller commits
    or rolls back.

    The published version is rewritten in place, as `invalid_model_changes`
    does, so interviews already running on it move too; older versions are
    history and left alone, so restoring one brings its models back and
    publishing it is refused.

    The draft is left alone. Test interviews run on it, and drafts are
    expected to sit on hosted models while the local one has little capacity;
    publishing switches them deliberately (`switch_models_to`). Rewriting the
    drafts too, as this once did, moved every project's tests onto the local
    model.

    Speech cannot be moved to another model: a published version with
    `with_audio` on while speech is test only is reported, and switched off
    with `disable_audio`.
    """
    llm = lib_settings.llm
    changes: list[str] = []
    rows = session.execute(
        text(
            "SELECT pv.id, pv.agent_configs, pv.config "
            "FROM projectcontent pc "
            "JOIN projectcontentversion pv ON pv.id = pc.published_version_id"
        )
    ).all()
    for version_id, agents_raw, config_raw in rows:
        data = _as_dict(agents_raw)
        if isinstance(data, dict):
            mutated = False
            for name, sub in data.items():
                if not isinstance(sub, dict) or "model" not in sub:
                    continue
                if not runs_on_answers(name, sub.get("include", True)):
                    continue
                if llm.is_test_only(sub["model"]):
                    changes.append(
                        f"projectcontentversion {version_id} "
                        f"agent_configs.{name}.model: {sub['model']!r} -> {to!r}"
                    )
                    sub["model"] = to
                    mutated = True
            if mutated:
                session.execute(
                    text(
                        "UPDATE projectcontentversion SET agent_configs = :v "
                        "WHERE id = :id"
                    ),
                    {"v": json.dumps(data), "id": version_id},
                )

        if not speech_is_test_only():
            continue
        config = _as_dict(config_raw)
        if not isinstance(config, dict) or not config.get("with_audio"):
            continue
        if not disable_audio:
            changes.append(
                f"projectcontentversion {version_id} config.with_audio: speech is "
                "test only; still closed to respondents (--disable-audio to switch "
                "it off)"
            )
            continue
        config["with_audio"] = False
        session.execute(
            text("UPDATE projectcontentversion SET config = :v WHERE id = :id"),
            {"v": json.dumps(config), "id": version_id},
        )
        changes.append(
            f"projectcontentversion {version_id} config.with_audio: True -> False"
        )

    return changes


@cli.command()
def fix_distributed_models(
    to: str = typer.Option(
        ...,
        "--to",
        help="The model to configure instead: one of [llm] models, which "
        "respondents' answers may be sent to.",
    ),
    disable_audio: bool = typer.Option(
        False,
        "--disable-audio",
        help="Also switch off audio answers on published projects while speech "
        "is test only.",
    ),
    dry_run: bool = False,
):
    """Move published versions off test-only models, so distributed interviews
    can start on them again. Drafts keep their models.

    See `distributed_model_changes` for what is rewritten and what is not.
    """
    if lib_settings.llm.is_test_only(to):
        typer.echo(
            f"{to!r} is not one of the distributable models: "
            f"{sorted(lib_settings.llm.models)}"
        )
        raise typer.Exit(1)

    prefix = "[DRY-RUN] " if dry_run else ""
    with Session(engine) as session:
        changes = distributed_model_changes(session, to, disable_audio)
        for change in changes:
            typer.echo(f"{prefix}{change}")
        if dry_run:
            session.rollback()
            typer.echo(f"[DRY-RUN] Would make {len(changes)} change(s).")
        else:
            session.commit()
            typer.echo(f"Made {len(changes)} change(s).")


@cli.command()
def export_messages(
    project_id: UUID,
    output: Path = typer.Option(
        None,
        "--output",
        "-o",
        help="Output xlsx path (default: ./{project_id}-messages.xlsx)",
    ),
):
    """Export all interview messages of a project to an xlsx file."""
    from .utils import messages_to_dataframe, write_messages_xlsx

    db = next(get_db())

    interviews, _ = db.interviews.get_interviews(project_id)

    messages = [
        message
        for interview in interviews
        for message in db.interviews.get_messages(interview.id, project_id)
    ]

    if not messages:
        typer.echo(
            typer.style(
                f"No messages found for project {project_id}.",
                fg=typer.colors.RED,
            )
        )
        raise typer.Exit(code=1)

    output = output or Path(f"{project_id}-messages.xlsx")

    df = messages_to_dataframe(messages)
    write_messages_xlsx(df, str(output))

    typer.echo(f"Exported {len(messages)} message(s) to {output}.")


if __name__ == "__main__":
    cli()
