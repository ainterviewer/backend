import json
from pathlib import Path
from uuid import UUID

import typer
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from typer import Typer

from ainterviewer.agents.config import AgentConfigs
from ainterviewer.settings import settings as lib_settings
from ainterviewer.types import DatabaseType

from ..dependencies import engine, get_db
from ..platform_release import Highlight, PlatformManifest
from ..settings import app_settings
from .models import UserCreate

cli = Typer()


@cli.command(hidden=True)
def _(): ...


@cli.command()
def setup_db(
    fresh: bool = typer.Option(
        False, "--fresh", help="Delete existing database before setup"
    ),
    users_file: Path = typer.Option(
        Path("storage/users/default.json"),
        "--users-file",
        help="Path to users JSON file to seed on creation",
    ),
):
    """Create database tables and seed initial users."""
    if app_settings.database.db == DatabaseType.SQLITE:
        db_dir = Path(app_settings.database.db_path)
        db_dir.mkdir(parents=True, exist_ok=True)

        if fresh:
            db_file = db_dir / app_settings.database.database_file  # ty:ignore[unsupported-operator]
            if db_file.exists():
                typer.confirm(f"Delete {db_file} and all its data?", abort=True)
                db_file.unlink()
                typer.echo(f"Deleted {db_file}.")

    db = next(get_db())
    db.create_db_and_tables()
    typer.echo("Database and tables created.")

    if users_file.exists():
        with users_file.open() as f:
            users = json.load(f)
        n = 0
        for user in users:
            try:
                db.users.create_user(UserCreate(**user))
                n += 1
            except IntegrityError:
                typer.echo(f"User {user['email']} already exists, skipping.")
        typer.echo(f"Created {n} user{'s' if n != 1 else ''}.")
    else:
        typer.echo(
            typer.style(
                f"No users file at {users_file}, skipping user creation.",
                fg=typer.colors.RED,
            )
        )


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
                # The draft's revision as well, as an ORM write would bump it,
                # so an editor that loaded the old model gets a conflict
                # rather than saving it back.
                bump = ", revision = revision + 1" if table == "projectcontent" else ""
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
