"""Relabel survey answers that a resumed interview stored as free text.

An interview that reconnected while a survey item was on screen replayed its
history and then waited for the answer. The wait went through
`AInterviewer.receive_data` with no message type, and the IO layer cannot work
one out for itself -- the respondent's client submits a closed answer in the
same frame as free text -- so the row landed as TEXT. Fixed in the library by
deriving the type from the turn the answer lands on; this repairs the rows
written before that.

What it looks for: a respondent message whose *preceding* message is an
interviewer turn carrying a survey item. That pairing is the same one
`app.db.survey_answers` and the browse query use -- a survey item is a snapshot
on the question, and the answer is the message after it.

Why it matters, given the answers themselves are not wrong: `message_type` is
what `DefaultChunkPolicy.should_embed_message` and its SQL mirror
`EmbeddingRepository._embeddable_conditions` gate on. So these rows are
embedded and keyword-searchable as if a respondent had written them, while
every correctly labelled sibling is excluded. Survey reports and cohort filters
read the question's survey item instead and are unaffected either way.

Embeddings: only MESSAGE chunks for the relabelled rows are dropped, and they
are dropped rather than re-derived -- under the policy they should never have
existed. QA-pair, section and interview chunks are untouched, because what goes
into those is decided by `Turn.is_free_text`, which reads the survey item off
the question and so was right all along. No backfill run is needed afterwards.

Dry run by default; pass --apply to write.

    uv run python scripts/relabel_resumed_survey_answers.py
    uv run python scripts/relabel_resumed_survey_answers.py --apply
"""

from __future__ import annotations

from collections import Counter
from uuid import UUID

import typer
from sqlalchemy import Text, and_, case, cast, delete, func, select, update
from sqlalchemy.orm import Session

from ainterviewer.types import EmbeddingKind, MessageRole, MessageType
from app.db.tables import EmbeddingTable, MessageTable
from app.dependencies import engine

cli = typer.Typer(pretty_exceptions_enable=False)

#: Rows are written in batches of this many ids, to keep the IN lists inside
#: what every driver will accept.
BATCH = 500


def _candidates(project_id: UUID | None, include_audio: bool):
    """Respondent rows whose question carried a survey item, mislabelled.

    The window runs over every message before any filtering: whether a
    respondent turn answered a survey item depends on the row *before* it, and
    filtering first would make "the row before" whichever row happened to
    survive. This is the same construction as
    `EmbeddingRepository._message_source`, for the same reason.

    A JSONB null is stored as the string ``null`` rather than SQL NULL, so the
    obvious IS NOT NULL test selects every row; it is compared as text instead.
    A boolean is lagged rather than the column, because `lag` returns the raw
    stored value and skips the column type's decoding.
    """
    survey_flag = case(
        (
            and_(
                MessageTable.survey_item.is_not(None),
                cast(MessageTable.survey_item, Text) != "null",
            ),
            1,
        ),
        else_=0,
    )
    interviewer_flag = case((MessageTable.role == MessageRole.ASSISTANT, 1), else_=0)

    window = {
        "partition_by": MessageTable.interview_id,
        "order_by": MessageTable.message_id,
    }

    source = select(
        MessageTable.id.label("id"),
        MessageTable.project_id.label("project_id"),
        MessageTable.interview_id.label("interview_id"),
        MessageTable.message_id.label("message_id"),
        MessageTable.role.label("role"),
        MessageTable.message_type.label("message_type"),
        MessageTable.content.label("content"),
        func.lag(survey_flag).over(**window).label("question_survey_flag"),
        func.lag(interviewer_flag).over(**window).label("previous_is_interviewer"),
    )

    if project_id is not None:
        source = source.where(MessageTable.project_id == project_id)

    source = source.subquery()

    # AUDIO is left alone by default. The live path does relabel a spoken
    # answer to a survey item -- the type it passes wins over the frame's --
    # but the recording is still referenced by `audio_file`, and rewriting that
    # row is a judgement about the data rather than a repair of this bug. The
    # dry run counts them separately so the call can be made with a number in
    # hand.
    mislabelled = (
        [MessageType.TEXT, MessageType.AUDIO] if include_audio else [MessageType.TEXT]
    )

    return select(
        source.c.id,
        source.c.project_id,
        source.c.interview_id,
        source.c.message_id,
        source.c.message_type,
        source.c.content,
    ).where(
        source.c.role == MessageRole.USER,
        source.c.message_type.in_(mislabelled),
        source.c.question_survey_flag == 1,
        source.c.previous_is_interviewer == 1,
    )


def _batched(ids: list[UUID]):
    for start in range(0, len(ids), BATCH):
        yield ids[start : start + BATCH]


@cli.command()
def main(
    apply: bool = typer.Option(
        False, "--apply", help="Write the changes. Without it, nothing is modified."
    ),
    project: str = typer.Option(None, "--project", help="Only this project id."),
    include_audio: bool = typer.Option(
        False,
        "--include-audio",
        help="Also relabel spoken answers to survey items (see the note in the source).",
    ),
    samples: int = typer.Option(10, "--samples", help="Example rows to print."),
):
    project_id = UUID(project) if project else None

    with Session(engine) as session:
        rows = session.execute(_candidates(project_id, include_audio)).all()

        if not rows:
            typer.echo("No mislabelled survey answers found.")
            return

        ids = [row.id for row in rows]
        by_project = Counter(str(row.project_id) for row in rows)
        by_type = Counter(str(row.message_type) for row in rows)
        interviews = {row.interview_id for row in rows}

        chunks = session.scalar(
            select(func.count())
            .select_from(EmbeddingTable)
            .where(
                EmbeddingTable.kind == EmbeddingKind.MESSAGE,
                EmbeddingTable.message_id.in_(ids),
            )
        )

        audio_only = 0
        if not include_audio:
            audio_only = len(
                session.execute(_candidates(project_id, include_audio=True)).all()
            ) - len(rows)

        typer.echo(f"{len(rows)} message(s) across {len(interviews)} interview(s)")
        for project_key, count in by_project.most_common():
            typer.echo(f"  project {project_key}: {count}")
        typer.echo(f"stored types: {dict(by_type)}")
        typer.echo(f"message embeddings to drop: {chunks}")
        if audio_only:
            typer.echo(
                f"note: {audio_only} spoken answer(s) to survey items left alone; "
                "--include-audio would relabel them too"
            )

        if samples:
            typer.echo("\nexamples:")
            for row in rows[:samples]:
                content = row.content.strip().replace("\n", " ")[:60]
                typer.echo(f"  {row.interview_id} #{row.message_id}: {content!r}")

        if not apply:
            typer.echo("\nDry run: nothing was modified. Pass --apply to write.")
            return

        updated = 0
        deleted = 0
        for batch in _batched(ids):
            deleted += session.execute(
                delete(EmbeddingTable).where(
                    EmbeddingTable.kind == EmbeddingKind.MESSAGE,
                    EmbeddingTable.message_id.in_(batch),
                )
            ).rowcount
            updated += session.execute(
                update(MessageTable)
                .where(MessageTable.id.in_(batch))
                .values(message_type=MessageType.SURVEY_ITEM)
            ).rowcount

        session.commit()

        typer.echo(
            f"\nRelabelled {updated} message(s); dropped {deleted} embedding(s)."
        )


if __name__ == "__main__":
    cli()
