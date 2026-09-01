"""Re-derive every embeddable chunk from stored interviews.

This is not a one-off migration tool. The live path emits chunks as an
interview runs, and it loses some by design: an abandoned interview never
reaches the point where its last question group is complete, a resumed one
replays its history without re-emitting, and anything dropped while the
embedding server was down stays dropped. The backfill is what makes those
recoverable, which is why it is worth running on a schedule and why the chunk
rules live in `ainterviewer.embedding` where both paths share them.

Idempotent: a chunk whose text and model are unchanged is skipped without
calling the model.
"""

import logging
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select

from ainterviewer.embedding import (
    DEFAULT_POLICY,
    ChunkPolicy,
    chunks_from_history,
    message_chunk,
)
from ainterviewer.interfaces import EmbeddingChunk
from ainterviewer.interview_guides.history import InterviewHistory

from ..db import InterviewDataBase
from ..db.tables import InterviewTable, MessageTable
from ..db.types import EmbeddingTask, InterviewType
from .client import EmbeddingClient, EmbeddingUnavailable
from .service import EmbeddingService

logger = logging.getLogger(__name__)


@dataclass
class BackfillReport:
    interviews: int = 0
    chunks_found: int = 0
    chunks_written: int = 0
    failed: list[tuple[UUID, str]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.interviews} interview(s), {self.chunks_found} chunk(s) found, "
            f"{self.chunks_written} written, {len(self.failed)} interview(s) failed"
        )


def chunks_for_interview(
    interview: InterviewTable,
    messages: list[MessageTable],
    policy: ChunkPolicy = DEFAULT_POLICY,
) -> list[EmbeddingChunk]:
    """Every chunk derivable from one stored interview.

    Message chunks are built straight from the rows, because that is where the
    message ids are. QA-pair and interview chunks come from the history
    reconstruction, because that is where the question grouping is -- the same
    reconstruction the interview loop uses to resume, and the same renderer it
    uses to emit chunks live.
    """
    chunks: list[EmbeddingChunk] = [
        chunk
        for message in messages
        if (
            chunk := message_chunk(
                project_id=interview.project_id,
                interview_id=interview.id,
                message_id=message.message_id,
                content=message.content,
                role=message.role,
                message_type=message.message_type,
                language=interview.language,
                section=message.section,
                main_question=message.main_question,
                sub_question=message.sub_question,
                skipped_by_condition=message.skipped_by_condition,
                policy=policy,
            )
        )
        is not None
    ]

    history = InterviewHistory()
    history.process_history(messages, interview.interview_guide)

    chunks.extend(
        chunks_from_history(
            history,
            project_id=interview.project_id,
            interview_id=interview.id,
            language=interview.language,
            policy=policy,
        )
    )

    return chunks


def pending_chunks(
    db: InterviewDataBase,
    *,
    task: EmbeddingTask = EmbeddingTask.DOCUMENT,
    model: str,
    project_id: UUID | None = None,
    policy: ChunkPolicy = DEFAULT_POLICY,
) -> tuple[list[EmbeddingChunk], list[tuple[UUID, str]]]:
    """Every chunk in scope that has no current vector, plus the interviews that
    could not be read.

    Deriving chunks is database work and takes seconds; embedding them is
    network work against a CPU inference server and takes minutes. Splitting the
    two lets a caller queue the slow half instead of holding a request, a
    threadpool worker, or a session open for the duration.
    """
    outstanding: list[EmbeddingChunk] = []
    failed: list[tuple[UUID, str]] = []

    for interview in _interviews(db, project_id=project_id):
        messages = _messages(db, interview.id)
        if not messages:
            continue

        try:
            chunks = chunks_for_interview(interview, messages, policy)
        except Exception as error:
            logger.warning(
                "Could not derive chunks for interview %s: %s", interview.id, error
            )
            failed.append((interview.id, str(error)))
            continue

        outstanding.extend(
            db.embeddings.needs_embedding(chunks, task=task, model=model)
        )

    return outstanding, failed


def rehydrate_text(
    db: InterviewDataBase,
    client: EmbeddingClient,
    *,
    project_id: UUID | None = None,
    policy: ChunkPolicy = DEFAULT_POLICY,
) -> int:
    """Fill in `EmbeddingTable.text` for rows stored before the column existed.

    Re-derives the chunks and writes the text onto rows whose content hash
    already matches, so no vector is recomputed and no text can land beside a
    vector made from something else. Rows that do not match are left alone for
    the ordinary backfill to re-embed.
    """
    filled = 0

    for interview in _interviews(db, project_id=project_id):
        messages = _messages(db, interview.id)
        if not messages:
            continue

        try:
            chunks = chunks_for_interview(interview, messages, policy)
        except Exception as error:
            logger.warning(
                "Could not derive chunks for interview %s: %s", interview.id, error
            )
            continue

        for chunk in chunks:
            # The same normalisation the service applies before hashing.
            text = client.truncate(chunk.text)
            key = db.embeddings.chunk_key(
                chunk, EmbeddingTask.DOCUMENT, client.settings.model
            )
            if db.embeddings.set_text(key, text):
                filled += 1

        db.session.commit()

    return filled


def _interviews(
    db: InterviewDataBase,
    *,
    project_id: UUID | None = None,
    interview_id: UUID | None = None,
    limit: int | None = None,
) -> list[InterviewTable]:
    statement = select(InterviewTable).where(
        InterviewTable.type != InterviewType.SYNTHETIC_TEST
    )
    if project_id is not None:
        statement = statement.where(InterviewTable.project_id == project_id)
    if interview_id is not None:
        statement = statement.where(InterviewTable.id == interview_id)
    statement = statement.order_by(InterviewTable.created_at)
    if limit is not None:
        statement = statement.limit(limit)

    return list(db.session.execute(statement).scalars().all())


def _messages(db: InterviewDataBase, interview_id: UUID) -> list[MessageTable]:
    return list(
        db.session.execute(
            select(MessageTable)
            .where(MessageTable.interview_id == interview_id)
            .order_by(MessageTable.message_id, MessageTable.created_at)
        )
        .scalars()
        .all()
    )


async def backfill(
    db: InterviewDataBase,
    client: EmbeddingClient,
    *,
    project_id: UUID | None = None,
    interview_id: UUID | None = None,
    limit: int | None = None,
    force: bool = False,
    dry_run: bool = False,
    policy: ChunkPolicy = DEFAULT_POLICY,
) -> BackfillReport:
    """Embed everything not already current.

    Synthetic test interviews are excluded here for the same reason the
    synthetic runner is given no embedder: they are model output, and mixing
    them into an analysis corpus would let generated answers surface as if they
    were things a respondent said.
    """
    report = BackfillReport()
    service = EmbeddingService(db, client)

    for interview in _interviews(
        db, project_id=project_id, interview_id=interview_id, limit=limit
    ):
        report.interviews += 1

        messages = _messages(db, interview.id)
        if not messages:
            continue

        try:
            chunks = chunks_for_interview(interview, messages, policy)
        except Exception as error:
            # A transcript the history reconstruction cannot parse. One bad
            # interview must not stop the run; it is reported instead.
            logger.warning(
                "Could not derive chunks for interview %s: %s", interview.id, error
            )
            report.failed.append((interview.id, str(error)))
            continue

        report.chunks_found += len(chunks)

        if dry_run or not chunks:
            continue

        try:
            report.chunks_written += await service.embed_and_store(chunks, force=force)
        except EmbeddingUnavailable as error:
            # One interview failing is not a reason to abandon the run -- a
            # single long transcript can time out while the server is otherwise
            # healthy. The circuit breaker is what distinguishes that from a
            # server that is actually down, so defer the decision to it.
            logger.warning("Failed to embed interview %s: %s", interview.id, error)
            report.failed.append((interview.id, str(error)))

            if client.circuit_open:
                logger.error(
                    "Embedding server marked unavailable; stopping after "
                    "%s interview(s)",
                    report.interviews,
                )
                break

    return report
