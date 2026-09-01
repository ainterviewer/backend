import asyncio
from uuid import UUID

import typer
from sqlalchemy.orm import Session
from typer import Typer

from ..db import InterviewDataBase
from ..dependencies import engine
from ..settings import app_settings
from .backfill import backfill as run_backfill
from .backfill import rehydrate_text as run_rehydrate_text
from .client import EmbeddingClient

cli = Typer()


@cli.command(hidden=True)
def _(): ...


@cli.command()
def status():
    """Show the embedding server's reachability and stored vector counts."""
    settings = app_settings.services.embedding
    client = EmbeddingClient()

    typer.echo(f"endpoint: {settings.endpoint or '(not configured)'}")
    typer.echo(f"enabled:  {settings.enabled}")
    typer.echo(f"model:    {settings.model} ({settings.dimension}d)")
    typer.echo(f"health:   {asyncio.run(client.health())}")

    with Session(engine) as session:
        db = InterviewDataBase(session)
        typer.echo(f"vectors:  {db.embeddings.count()}")


@cli.command()
def rehydrate_text(
    project: str = typer.Option(None, "--project", help="Only this project id."),
):
    """Fill in stored chunk text for rows embedded before the text column
    existed. Calls the embedding model zero times."""
    client = EmbeddingClient()

    with Session(engine) as session:
        db = InterviewDataBase(session)
        filled = run_rehydrate_text(
            db, client, project_id=UUID(project) if project else None
        )

    typer.echo(f"Filled text on {filled} row(s).")


@cli.command()
def backfill(
    project: str = typer.Option(None, "--project", help="Only this project id."),
    interview: str = typer.Option(None, "--interview", help="Only this interview id."),
    limit: int = typer.Option(None, "--limit", help="Stop after this many interviews."),
    force: bool = typer.Option(
        False, "--force", help="Re-embed even where the stored vector is current."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Count the chunks without calling the model."
    ),
):
    """Embed every chunk derivable from stored interviews that lacks a current
    vector. Synthetic test interviews are always excluded."""

    async def main():
        client = EmbeddingClient()
        if not dry_run and not client.enabled:
            raise typer.BadParameter(
                "Embedding is disabled or has no endpoint; set "
                "APP_SERVICE__EMBEDDING__ENABLED and __ENDPOINT."
            )

        with Session(engine) as session:
            db = InterviewDataBase(session)
            report = await run_backfill(
                db,
                client,
                project_id=UUID(project) if project else None,
                interview_id=UUID(interview) if interview else None,
                limit=limit,
                force=force,
                dry_run=dry_run,
            )

        await client.aclose()
        return report

    report = asyncio.run(main())
    typer.echo(report.summary())
    for interview_id, error in report.failed:
        typer.echo(f"  failed {interview_id}: {error}")


if __name__ == "__main__":
    cli()
