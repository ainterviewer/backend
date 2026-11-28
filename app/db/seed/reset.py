"""Reset the configured SQLite database and install development fixtures."""

from contextlib import nullcontext
from pathlib import Path

import typer
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from ainterviewer.types import DatabaseType
from app.db.crud import InterviewDataBase
from app.dependencies import engine
from app.settings import app_settings

from .accounts import DEMO_EMAIL, DEMO_PASSWORD, DEV_EMAIL, DEV_PASSWORD
from .accounts import create as create_accounts
from .ids import stable_ids
from .projects import create as create_projects
from .releases import create as create_releases


def reset_and_seed(*, yes: bool, random_uuids: bool = False) -> None:
    """Replace the configured local SQLite database with development fixtures."""
    if app_settings.database.db is not DatabaseType.SQLITE:
        raise typer.BadParameter("seed-dev only supports a SQLite development database")
    db_file = Path(app_settings.database.db_path) / "db.sqlite"
    if not yes:
        typer.confirm(f"Replace {db_file} and all of its data?", abort=True)
    engine.dispose()
    db_file.parent.mkdir(parents=True, exist_ok=True)
    for path in (db_file, Path(f"{db_file}-wal"), Path(f"{db_file}-shm")):
        path.unlink(missing_ok=True)
    with Session(engine) as session:
        db = InterviewDataBase(session)
        db.create_db_and_tables()
        with nullcontext() if random_uuids else stable_ids(session):
            create_releases(session)
            create_projects(db, session, create_accounts(db, session))
    if not inspect(engine).has_table("project"):
        raise RuntimeError(
            "Development database setup did not create the project table"
        )
    typer.echo(
        f"Seeded {db_file} with 8 projects and 5 platform releases. "
        f"Sign in as {DEV_EMAIL} / {DEV_PASSWORD}, "
        f"or as the demo account {DEMO_EMAIL} / {DEMO_PASSWORD}."
    )
