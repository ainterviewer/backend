"""Storage and search for embedding vectors."""

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import numpy as np
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import joinedload

from ainterviewer.interfaces import EmbeddingChunk
from ainterviewer.lpm.types import CustomToken
from ainterviewer.types import EmbeddingKind, InterviewStatus, MessageRole

from ...types import TurnRole
from ..models import EmbeddingTurn
from ..tables import (
    EmbeddingTable,
    InterviewTable,
    MessageTable,
    ProjectParticipantTable,
)
from ..types import EmbeddingTask, InterviewType
from .base import BaseRepository

# Stored vectors are raw little-endian float32, `dim` of them. They are L2
# normalised by the embedding server, so a dot product *is* the cosine
# similarity and nothing needs to renormalise on the way out.
VECTOR_DTYPE = np.float32

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmbeddingFilters:
    """Everything a search can be narrowed by, beyond the vector itself.

    All of it is ordinary SQL applied *before* scoring, which is the advantage
    an exact scan has over an ANN index: the candidate set is whatever the
    filters say it is, and a page of ten is ten of them.
    """

    interview_ids: list[UUID] | None = None
    #: Restrict to these languages. A list rather than one code because a
    #: multilingual project is often analysed two languages at a time -- the
    #: ones with enough respondents to say anything -- not all or one.
    languages: list[str] | None = None
    status: InterviewStatus | None = None
    participant_id: UUID | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None
    include_synthetic: bool = False
    #: Restrict to these places in the interview guide, as ``(section,
    #: main_question)`` pairs -- the same coordinates :class:`ChunkCoordinates`
    #: groups by, and the same pairs the annotate view filters messages with.
    #:
    #: A section is selected by listing every one of its questions rather than
    #: by a separate section filter: the caller already holds the guide and so
    #: knows what a section contains, and one shape of filter is easier to
    #: reason about than two that can disagree.
    #:
    #: Note that INTERVIEW chunks carry no guide coordinates at all -- a whole
    #: transcript spans the guide -- so any question filter excludes them
    #: entirely. That is the honest answer rather than a bug: there is no
    #: subset of an interview-level vector belonging to one question.
    questions: list[tuple[int, int]] | None = None


@dataclass(frozen=True)
class EmbeddingSearchHit:
    embedding: EmbeddingTable
    score: float


@dataclass(frozen=True)
class EmbeddingSearchPage:
    """One page of a ranked scan, with the size of the ranking behind it.

    Both counts come from the scan itself rather than a second `COUNT(*)`: the
    candidate rows are already in memory to be scored, so counting them is free
    and cannot disagree with what was ranked. `scored` is every chunk the query
    was compared against; `total` is how many of those could be returned, which
    is one fewer whenever the source chunk of a "more like this" survived the
    filters.
    """

    hits: list[EmbeddingSearchHit]
    scored: int
    total: int


@dataclass(frozen=True)
class ChunkCoordinates:
    """Where a chunk sits in the interview guide, and what language it is in.

    Everything about a chunk that clustering can group by without reading its
    text. Carried alongside the vectors because a cluster only means something
    read against these: a cluster that is one question, or one language, has
    found the scaffolding rather than anything a respondent said.
    """

    section: int | None
    main_question: int | None
    sub_question: int | None
    language: str

    @property
    def question(self) -> tuple[int | None, int | None]:
        """The question a chunk belongs to, not the probe within it.

        A MESSAGE chunk carries a `sub_question`; grouping by it would put every
        turn of one question in a group of its own.
        """
        return (self.section, self.main_question)


@dataclass(frozen=True)
class PendingEmbedding:
    """A chunk that has been embedded and is ready to store."""

    chunk: EmbeddingChunk
    text: str
    vector: list[float]


def encode_vector(vector: list[float] | np.ndarray) -> bytes:
    return np.asarray(vector, dtype=VECTOR_DTYPE).tobytes()


def decode_vector(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=VECTOR_DTYPE)


class EmbeddingRepository(BaseRepository):
    """Reads and writes `EmbeddingTable`.

    Search is an exact brute-force scan: the candidate vectors for one project
    and kind are read, stacked, and scored with a single matrix product. That is
    a deliberate choice over the `sqlite-vector` ANN index, which returns a
    *global* top-k and cannot be filtered -- there is no way to scope
    `vector_quantize_scan` to a project, let alone to a date range or an
    annotation. Over-fetching and filtering afterwards gives no guarantee of
    returning k results. At this corpus size the scan costs single-digit
    milliseconds and every SQL filter stays available; if one project ever grows
    past roughly 50k chunks, this method is the seam to put an ANN index behind.
    """

    # ------------------------------------------------------------------ #
    # Keys                                                               #
    # ------------------------------------------------------------------ #

    @staticmethod
    def chunk_key(chunk: EmbeddingChunk, task: EmbeddingTask, model: str) -> str:
        """The identity of a chunk variant, as one string.

        A composite unique constraint cannot do this job: `message_id` and the
        structural coordinates are NULL for some kinds, and NULLs compare as
        distinct in a UNIQUE constraint on both SQLite and PostgreSQL, so the
        constraint would stop deduplicating precisely where it is needed.
        """
        parts = [
            chunk.kind.value,
            str(chunk.interview_id),
            "" if chunk.message_id is None else str(chunk.message_id),
            "" if chunk.section is None else str(chunk.section),
            "" if chunk.main_question is None else str(chunk.main_question),
            "" if chunk.sub_question is None else str(chunk.sub_question),
            task.value,
            model,
        ]
        return "|".join(parts)

    @staticmethod
    def content_hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------ #
    # Writing                                                            #
    # ------------------------------------------------------------------ #

    def get_existing_state(
        self,
        chunk_keys: list[str],
    ) -> dict[str, tuple[str, str]]:
        """Map chunk_key -> (content_hash, format_version) for stored keys."""
        if not chunk_keys:
            return {}

        rows = self.session.execute(
            select(
                EmbeddingTable.chunk_key,
                EmbeddingTable.content_hash,
                EmbeddingTable.format_version,
            ).where(EmbeddingTable.chunk_key.in_(chunk_keys))
        ).all()
        return {key: (content_hash, version) for key, content_hash, version in rows}

    def needs_embedding(
        self,
        chunks: list[EmbeddingChunk],
        *,
        task: EmbeddingTask,
        model: str,
    ) -> list[EmbeddingChunk]:
        """Filter `chunks` down to those with no current vector.

        A chunk is current when a row exists for its key whose `content_hash`
        matches the text it would be embedded from now *and* whose
        `format_version` matches the policy that produced it. The hash alone is
        not enough: a policy can change which chunks are included without
        changing how the ones that survive are rendered, and those rows would
        otherwise look current forever.
        """
        keys = [self.chunk_key(chunk, task, model) for chunk in chunks]
        stored = self.get_existing_state(keys)

        return [
            chunk
            for chunk, key in zip(chunks, keys)
            if stored.get(key) != (self.content_hash(chunk.text), chunk.format_version)
        ]

    def store(
        self,
        pending: list[PendingEmbedding],
        *,
        task: EmbeddingTask = EmbeddingTask.DOCUMENT,
        model: str,
        commit: bool = True,
    ) -> int:
        """Insert or replace vectors. Returns the number of rows written."""
        if not pending:
            return 0

        keys = [self.chunk_key(item.chunk, task, model) for item in pending]

        # Replace rather than update in place: the row is small, and a stale
        # vector left behind by a partial update would be indistinguishable
        # from a current one.
        self.session.execute(
            delete(EmbeddingTable).where(EmbeddingTable.chunk_key.in_(keys))
        )

        written = 0

        for item, key in zip(pending, keys):
            chunk = item.chunk
            message_id = self._resolve_message_id(chunk)

            if chunk.kind == EmbeddingKind.MESSAGE and message_id is None:
                # A per-message vector that points at no message would be
                # unreachable from the transcript and undeletable with it.
                # Dropping it is better than storing it detached.
                logger.warning(
                    "No message row for interview %s message_id %s; "
                    "skipping its embedding",
                    chunk.interview_id,
                    chunk.message_id,
                )
                continue

            written += 1
            self.session.add(
                EmbeddingTable(
                    kind=chunk.kind,
                    task=task,
                    project_id=chunk.project_id,
                    interview_id=chunk.interview_id,
                    message_id=message_id,
                    section=chunk.section,
                    main_question=chunk.main_question,
                    sub_question=chunk.sub_question,
                    language=chunk.language,
                    model=model,
                    format_version=chunk.format_version,
                    dim=len(item.vector),
                    chunk_key=key,
                    content_hash=self.content_hash(item.text),
                    text=item.text,
                    vector=encode_vector(item.vector),
                )
            )

        if commit:
            self.session.commit()

        return written

    def _resolve_message_id(self, chunk: EmbeddingChunk) -> UUID | None:
        """Resolve a MESSAGE chunk's per-interview counter to a message row.

        Chunks identify themselves structurally because `InterviewHistory` holds
        no row ids (see `EmbeddingChunk`). `(interview_id, message_id)` is unique
        on `message`, so this is exact rather than a best guess.
        """
        if chunk.kind != EmbeddingKind.MESSAGE or chunk.message_id is None:
            return None

        return self.session.execute(
            select(MessageTable.id).where(
                MessageTable.interview_id == chunk.interview_id,
                MessageTable.message_id == chunk.message_id,
            )
        ).scalar_one_or_none()

    def delete_for_interviews(self, interview_ids: list[UUID]) -> int:
        if not interview_ids:
            return 0
        result = self.session.execute(
            delete(EmbeddingTable).where(EmbeddingTable.interview_id.in_(interview_ids))
        )
        return result.rowcount or 0  # ty: ignore[unresolved-attribute]

    # ------------------------------------------------------------------ #
    # Reading                                                            #
    # ------------------------------------------------------------------ #

    def _candidate_statement(
        self,
        *,
        project_id: UUID,
        kind: EmbeddingKind,
        task: EmbeddingTask,
        filters: EmbeddingFilters,
    ):
        """Select (id, vector) for everything in scope.

        Only the two columns the scan needs: the full text and metadata are
        fetched for the k winners afterwards, so a large candidate set costs a
        vector read each, not a row read each.
        """
        statement = select(EmbeddingTable.id, EmbeddingTable.vector).where(
            EmbeddingTable.project_id == project_id,
            EmbeddingTable.kind == kind,
            EmbeddingTable.task == task,
        )

        if filters.interview_ids is not None:
            statement = statement.where(
                EmbeddingTable.interview_id.in_(filters.interview_ids)
            )

        if filters.languages:
            statement = statement.where(EmbeddingTable.language.in_(filters.languages))

        if filters.questions:
            # An OR of pairs rather than a row-value `IN`: the list is a handful
            # of questions picked by hand, so the planner sees the same thing
            # either way, and this stays true on every backend rather than only
            # the one that supports row constructors.
            statement = statement.where(
                or_(
                    *(
                        and_(
                            EmbeddingTable.section == section,
                            EmbeddingTable.main_question == main_question,
                        )
                        for section, main_question in filters.questions
                    )
                )
            )

        interview_conditions = []
        if not filters.include_synthetic:
            interview_conditions.append(
                InterviewTable.type != InterviewType.SYNTHETIC_TEST
            )
        if filters.status is not None:
            interview_conditions.append(InterviewTable.status == filters.status)
        if filters.participant_id is not None:
            interview_conditions.append(
                InterviewTable.participant_id == filters.participant_id
            )
        if filters.created_after is not None:
            interview_conditions.append(
                InterviewTable.created_at >= filters.created_after
            )
        if filters.created_before is not None:
            interview_conditions.append(
                InterviewTable.created_at <= filters.created_before
            )

        if interview_conditions:
            statement = statement.where(
                EmbeddingTable.interview_id.in_(
                    select(InterviewTable.id).where(*interview_conditions)
                )
            )

        return statement

    def previews(self, ids: list[UUID], chars: int) -> dict[UUID, str]:
        """Short excerpts, for hover text on a plot of many points.

        A scatter wants a line per point, not the whole chunk: sending the full
        text of every point would multiply the payload by an order of magnitude
        for text almost none of which is read.
        """
        if not ids:
            return {}

        rows = self.session.execute(
            select(EmbeddingTable.id, func.substr(EmbeddingTable.text, 1, chars)).where(
                EmbeddingTable.id.in_(ids)
            )
        ).all()
        return {row[0]: row[1] for row in rows if row[1]}

    def hydrate(self, ids: list[UUID]) -> dict[UUID, EmbeddingTable]:
        """Public form of `_hydrate`, for callers holding ids from a scan."""
        return self._hydrate(ids)

    def turns_for(
        self, embeddings: Sequence[EmbeddingTable]
    ) -> dict[UUID, list[EmbeddingTurn]]:
        """The messages behind each chunk, as speaker turns.

        Rendering a result the way the interview read it needs to know who said
        what, and the stored chunk text cannot say: it is one string built for
        the model, and splitting it back on its ``Q:``/``A:`` prefixes is a
        parse of prose that any respondent can break by starting a sentence
        with "Q:". The message rows carry the roles structurally, so they are
        the source here.

        INTERVIEW chunks get no turns. A whole transcript rendered as bubbles in
        a result list is the transcript view, which every hit already links to,
        and shipping one per hit would dwarf the rest of the response.

        One query for every hit on the page, grouped in Python: the alternative
        is a query per chunk, and a page of ten results with three
        representatives per cluster makes that dozens of round trips.
        """
        wanted = [
            embedding
            for embedding in embeddings
            if embedding.kind in (EmbeddingKind.QA_PAIR, EmbeddingKind.MESSAGE)
        ]
        if not wanted:
            return {}

        rows = self.session.execute(
            select(
                MessageTable.id,
                MessageTable.interview_id,
                MessageTable.role,
                MessageTable.content,
                MessageTable.section,
                MessageTable.main_question,
                MessageTable.survey_item,
                MessageTable.skipped_by_condition,
            )
            .where(
                MessageTable.interview_id.in_({e.interview_id for e in wanted}),
                MessageTable.role != MessageRole.SYSTEM,
                MessageTable.section.is_not(None),
                MessageTable.main_question.is_not(None),
            )
            .order_by(MessageTable.interview_id, MessageTable.message_id)
        ).all()

        # (interview, section, main_question) -> the group's messages, in order.
        # Keyed on the question group rather than on the interview because that
        # is the unit both remaining kinds are about: a QA pair *is* the group,
        # and a message is one turn inside one.
        grouped: dict[tuple[UUID, int, int], list[Any]] = {}
        for row in rows:
            if row.skipped_by_condition or not row.content.strip():
                continue
            if row.content.strip() in CustomToken:
                continue
            grouped.setdefault(
                (row.interview_id, row.section, row.main_question), []
            ).append(row)

        turns: dict[UUID, list[EmbeddingTurn]] = {}
        for embedding in wanted:
            if embedding.section is None or embedding.main_question is None:
                continue
            group = grouped.get(
                (embedding.interview_id, embedding.section, embedding.main_question)
            )
            if not group:
                continue

            rendered: list[EmbeddingTurn] = []
            # An interviewer turn's survey item is what makes the answer after
            # it a click rather than a sentence, and it is the question row that
            # carries it.
            pending_item: str | None = None
            for row in group:
                respondent = MessageRole(row.role) == MessageRole.USER
                item = row.survey_item.type if row.survey_item else None
                rendered.append(
                    EmbeddingTurn(
                        role=(
                            TurnRole.RESPONDENT if respondent else TurnRole.INTERVIEWER
                        ),
                        text=row.content.strip(),
                        survey_label=(item or pending_item) if respondent else None,
                        # Only a MESSAGE chunk singles a turn out; for a QA pair
                        # the whole group is the chunk.
                        match=(
                            embedding.kind == EmbeddingKind.MESSAGE
                            and row.id == embedding.message_id
                        ),
                    )
                )
                pending_item = None if respondent else item

            if embedding.kind == EmbeddingKind.MESSAGE:
                # Everything after the embedded message answers a later probe
                # and is not what this chunk says; what came before it is the
                # question, and is.
                matched = next(
                    (i for i, turn in enumerate(rendered) if turn.match), None
                )
                if matched is None:
                    continue
                rendered = rendered[: matched + 1]

            turns[embedding.id] = rendered

        return turns

    def _hydrate(self, ids: list[UUID]) -> dict[UUID, EmbeddingTable]:
        """Load the winning rows with the interview and participant a result row
        needs, so a client is not left making one request per hit."""
        return {
            embedding.id: embedding
            for embedding in self.session.execute(
                select(EmbeddingTable)
                .where(EmbeddingTable.id.in_(ids))
                .options(
                    joinedload(EmbeddingTable.interview)
                    .joinedload(InterviewTable.project_participant)
                    .joinedload(ProjectParticipantTable.participant)
                )
            )
            .unique()
            .scalars()
        }

    def _rank(
        self,
        rows: Sequence[Any],
        query_vector: list[float] | np.ndarray,
        limit: int,
        offset: int = 0,
        exclude: UUID | None = None,
    ) -> EmbeddingSearchPage:
        """Score every candidate, return one page of the ranking.

        Paging re-scores the whole candidate set on every request. That is the
        same work the first page does -- one matrix product over a few thousand
        rows, single-digit milliseconds -- and it keeps a page a pure function
        of the query and the corpus, with no ranking to cache, invalidate or
        pin to a session.
        """
        scored = len(rows)
        if exclude is not None:
            rows = [row for row in rows if row[0] != exclude]
        if not rows or offset >= len(rows):
            return EmbeddingSearchPage(hits=[], scored=scored, total=len(rows))

        query = np.asarray(query_vector, dtype=VECTOR_DTYPE)
        matrix = np.frombuffer(
            b"".join(blob for _, blob in rows), dtype=VECTOR_DTYPE
        ).reshape(len(rows), -1)

        if matrix.shape[1] != query.shape[0]:
            raise ValueError(
                f"Query has {query.shape[0]} dimensions but stored vectors have "
                f"{matrix.shape[1]}; the model or its dimension has changed and "
                "the corpus needs re-embedding"
            )

        scores = matrix @ query

        # Deep enough to reach the far end of the requested page, no deeper:
        # argpartition finds the top `depth` without sorting the whole array,
        # the slice is sorted so the caller gets them best-first, and the page
        # is taken off the front of that.
        depth = min(offset + limit, len(rows))
        top = np.argpartition(-scores, depth - 1)[:depth]
        top = top[np.argsort(-scores[top])][offset:]

        embeddings = self._hydrate([rows[i][0] for i in top])

        return EmbeddingSearchPage(
            hits=[
                EmbeddingSearchHit(
                    embedding=embeddings[rows[i][0]], score=float(scores[i])
                )
                for i in top
                if rows[i][0] in embeddings
            ],
            scored=scored,
            total=len(rows),
        )

    def search(
        self,
        *,
        project_id: UUID,
        query_vector: list[float] | np.ndarray,
        kind: EmbeddingKind = EmbeddingKind.QA_PAIR,
        task: EmbeddingTask = EmbeddingTask.DOCUMENT,
        limit: int = 10,
        offset: int = 0,
        filters: EmbeddingFilters | None = None,
    ) -> EmbeddingSearchPage:
        """Exact nearest-neighbour search within one project and chunk kind."""
        rows = self.session.execute(
            self._candidate_statement(
                project_id=project_id,
                kind=kind,
                task=task,
                filters=filters or EmbeddingFilters(),
            )
        ).all()

        return self._rank(rows, query_vector, limit, offset)

    def similar_to(
        self,
        *,
        embedding_id: UUID,
        limit: int = 10,
        offset: int = 0,
        filters: EmbeddingFilters | None = None,
    ) -> tuple[EmbeddingTable, EmbeddingSearchPage]:
        """Nearest neighbours of a chunk already in the corpus.

        Costs no inference at all -- the query vector is the stored one -- which
        is what makes "more like this" the cheapest exploratory gesture
        available. Searches within the source's own project and kind, and never
        returns the source itself.
        """
        source = self.session.get(EmbeddingTable, embedding_id)
        if source is None:
            raise NoResultFound(f"No embedding {embedding_id}")

        rows = self.session.execute(
            self._candidate_statement(
                project_id=source.project_id,
                kind=source.kind,
                task=source.task,
                filters=filters or EmbeddingFilters(),
            )
        ).all()

        return source, self._rank(
            rows, decode_vector(source.vector), limit, offset, exclude=source.id
        )

    def vectors_for(
        self,
        *,
        project_id: UUID,
        kind: EmbeddingKind = EmbeddingKind.QA_PAIR,
        task: EmbeddingTask = EmbeddingTask.DOCUMENT,
        filters: EmbeddingFilters | None = None,
    ) -> tuple[list[UUID], np.ndarray, list[ChunkCoordinates]]:
        """Every vector in scope, stacked, with each one's coordinates. The
        input to clustering.

        The coordinates are returned alongside because clustering needs to know
        which chunks share a question or a language -- both to report how far a
        cluster is from being just that group, and to centre it out -- and
        because the same scatter can then be coloured by the guide instead of by
        what clustering found, which is the comparison that says whether a
        cluster is a theme or just scaffolding.
        """
        statement = self._candidate_statement(
            project_id=project_id,
            kind=kind,
            task=task,
            filters=filters or EmbeddingFilters(),
        ).add_columns(
            EmbeddingTable.section,
            EmbeddingTable.main_question,
            EmbeddingTable.sub_question,
            EmbeddingTable.language,
        )

        rows = self.session.execute(statement).all()

        if not rows:
            return [], np.empty((0, 0), dtype=VECTOR_DTYPE), []

        matrix = np.frombuffer(
            b"".join(row[1] for row in rows), dtype=VECTOR_DTYPE
        ).reshape(len(rows), -1)

        return (
            [row[0] for row in rows],
            matrix,
            [ChunkCoordinates(row[2], row[3], row[4], str(row[5])) for row in rows],
        )

    def set_text(self, chunk_key: str, text: str) -> bool:
        """Fill in the stored text for a row that predates the column.

        Only ever called where the content hash already matches, so this cannot
        put text next to a vector that was made from something else.
        """
        result = self.session.execute(
            update(EmbeddingTable)
            .where(
                EmbeddingTable.chunk_key == chunk_key,
                EmbeddingTable.content_hash == self.content_hash(text),
                EmbeddingTable.text.is_(None),
            )
            .values(text=text)
        )
        return bool(result.rowcount)  # ty: ignore[unresolved-attribute]

    def coverage(self, project_id: UUID) -> dict[str, int]:
        """Stored vector counts per kind, for one project."""
        rows = self.session.execute(
            select(EmbeddingTable.kind, func.count(EmbeddingTable.id))
            .where(EmbeddingTable.project_id == project_id)
            .group_by(EmbeddingTable.kind)
        ).all()
        return {kind.value: count for kind, count in rows}

    def languages(self, project_id: UUID) -> dict[str, int]:
        """Stored vector counts per language, for one project.

        Counted here rather than derived by the client from a result set: a
        client holds whatever one projection returned under whatever filters
        were active, so it cannot tell a language the project does not have
        from one the current filters excluded -- and a language filter built
        on that would delete its own options as soon as it was used.
        """
        rows = self.session.execute(
            select(EmbeddingTable.language, func.count(EmbeddingTable.id))
            .where(EmbeddingTable.project_id == project_id)
            .group_by(EmbeddingTable.language)
            .order_by(func.count(EmbeddingTable.id).desc())
        ).all()
        return {language: count for language, count in rows}

    def count(self) -> int:
        return (
            self.session.execute(select(func.count(EmbeddingTable.id))).scalar_one()
            or 0
        )
