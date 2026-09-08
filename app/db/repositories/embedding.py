"""Storage and search for embedding vectors."""

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID, uuid5

import numpy as np
from sqlalchemy import Text, and_, case, cast, delete, func, or_, select, update
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import joinedload

from ainterviewer.interfaces import EmbeddingChunk
from ainterviewer.lpm.types import CustomToken
from ainterviewer.types import (
    EmbeddingKind,
    InterviewStatus,
    MessageRole,
    MessageType,
)

from ...types import TurnRole
from ..keyword_query import (
    Scope,
    compile_condition,
    excluded_spans,
    match_spans,
    parse,
)
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
    #: Restrict to chunks whose underlying respondent messages contain this
    #: text. Always evaluated against ``message.content`` rather than against
    #: the stored chunk text, so it means the same thing on an embedded project
    #: and an un-embedded one -- the browse path has no chunk text to match.
    #:
    #: A boolean query rather than a literal string: `dog OR cat`,
    #: `kids -school`, `(dog OR cat) AND "my neighbour"`. See
    #: :mod:`app.db.keyword_query` for the grammar. Always case-insensitive.
    keyword: str | None = None
    #: Which side of the exchange a bare term is matched against.
    #:
    #: "answer" is the default and the historical behaviour: a chunk restates
    #: the question it answers, so counting a word the interviewer said would
    #: let the guide's own phrasing look like a finding. "question" and "both"
    #: are asked for deliberately, and `q:`/`a:` in the query override this for
    #: a single term.
    keyword_scope: Scope = "answer"


def _keyword_node(filters: EmbeddingFilters):
    """The keyword query as a tree, or None where it asks for nothing.

    One place, because the condition that selects rows and the spans that mark
    them have to be reading the same query -- that is the whole reason
    highlighting moved to the server.
    """
    if not filters.keyword or not filters.keyword.strip():
        return None
    return parse(filters.keyword)


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


class ChunkLike(Protocol):
    """The coordinates a chunk must carry to be rendered as turns.

    `turns_for` reads coordinates, never rows: it is given both real
    `EmbeddingTable` rows and the `BrowseUnit`s assembled for un-embedded
    corpora, and demanding the table type would tie transcript rendering to
    having been embedded -- exactly the tie browsing exists to cut.
    """

    @property
    def id(self) -> UUID: ...
    @property
    def kind(self) -> EmbeddingKind: ...
    @property
    def interview_id(self) -> UUID: ...
    @property
    def message_id(self) -> UUID | None: ...
    @property
    def section(self) -> int | None: ...
    @property
    def main_question(self) -> int | None: ...


@dataclass
class BrowseUnit:
    """One unit of the corpus assembled from message rows rather than vectors.

    Deliberately shaped like an `EmbeddingTable` row, because `turns_for` and
    `EmbeddingSearchHit.from_hit` only ever read attributes: giving browsing the
    same attribute names lets both reuse the rendering the search path already
    has, instead of growing a second one that can disagree with it.

    ``id`` is the real embedding id when the unit has been embedded, and a
    deterministic UUID5 of its coordinates when it has not -- stable across
    requests, so a client can select and page without a row changing identity.
    ``embedded`` is what says which, and so whether "more like this" can be
    asked of it at all.
    """

    id: UUID
    kind: EmbeddingKind
    interview_id: UUID
    message_id: UUID | None
    section: int | None
    main_question: int | None
    sub_question: int | None
    language: str
    text: str | None
    interview: Any
    embedded: bool


@dataclass(frozen=True)
class BrowsePage:
    """One page of a browse, and how many units it was cut from."""

    units: list[BrowseUnit]
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

    # ------------------------------------------------------------------ #
    # Browsing (no vectors)                                              #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _has_survey_item(column):
        """Whether a message carries a survey item.

        Not `column.is_(None)`, which never matches: `PydanticJSONB` writes JSON
        ``null`` rather than SQL NULL, so every row is non-NULL and the obvious
        test silently selects nothing. Checked as text because that is the one
        reading both a JSON null and a real object answer honestly.
        """
        return and_(column.is_not(None), cast(column, Text) != "null")

    def _message_source(self, project_id: UUID):
        """Every message of a project, each carrying its question's survey flag.

        The window is what makes the policy expressible in SQL at all. Whether a
        respondent turn is free text depends on the *question* that drew it --
        `Turn.is_free_text` reads the survey item off the question, not off the
        answer -- and in the message table that question is the row before it.
        So the lag runs over every message in the interview, before any
        filtering: filter first and the row before a respondent turn would be
        whichever row happened to survive, which is not the question.

        A boolean is lagged rather than the column itself, because `lag` returns
        the raw stored value and skips the column type's own decoding -- the
        JSONB would arrive as the string "null" and compare equal to nothing.
        """
        survey_flag = case(
            (self._has_survey_item(MessageTable.survey_item), 1), else_=0
        )
        # The interviewer turn before a respondent one is the question it
        # answers -- the same row `turns_for` renders above the answer, and the
        # same definition `Turn.is_free_text` uses. Lagged over every message
        # before any filtering, for the reason above.
        # ASSISTANT is the interviewer; `turns_for` draws the same line, by
        # calling everything that is not USER an interviewer turn.
        interviewer_flag = case(
            (MessageTable.role == MessageRole.ASSISTANT, 1), else_=0
        )
        previous_content = func.lag(MessageTable.content).over(
            partition_by=MessageTable.interview_id,
            order_by=MessageTable.message_id,
        )
        previous_is_interviewer = func.lag(interviewer_flag).over(
            partition_by=MessageTable.interview_id,
            order_by=MessageTable.message_id,
        )
        return (
            select(
                MessageTable.id,
                MessageTable.interview_id,
                MessageTable.message_id,
                MessageTable.role,
                MessageTable.message_type,
                MessageTable.content,
                MessageTable.section,
                MessageTable.main_question,
                MessageTable.sub_question,
                MessageTable.skipped_by_condition,
                survey_flag.label("survey_flag"),
                func.lag(survey_flag)
                .over(
                    partition_by=MessageTable.interview_id,
                    order_by=MessageTable.message_id,
                )
                .label("question_survey_flag"),
                # NULL where the row before was not an interviewer turn -- a
                # respondent writing twice running answers no new question, and
                # a question-scoped term must not match the previous answer.
                case(
                    (previous_is_interviewer == 1, previous_content), else_=None
                ).label("question_content"),
            )
            .where(MessageTable.project_id == project_id)
            .subquery()
        )

    @staticmethod
    def _embeddable_conditions(source):
        """The SQL mirror of `DefaultChunkPolicy.should_embed_message`.

        A second expression of a rule that already lives in the library, which
        is a real cost and worth naming: the policy is written against
        `InterviewHistory` domain objects and browsing has only message rows.
        It is worth paying because browsing has to work on a project nobody has
        embedded -- otherwise keyword search would be a feature you unlock by
        running a backfill, which is not what it is.

        `TestChunkPolicy` in `tests/test_browse.py` pins the two together, case
        by case: every rule the library's policy decides is restated there
        against this. When the policy moves and this does not, those are what
        say so.
        """
        return (
            source.c.role == MessageRole.USER,
            source.c.message_type.in_([MessageType.TEXT, MessageType.AUDIO]),
            source.c.skipped_by_condition.is_(False),
            source.c.section.is_not(None),
            source.c.main_question.is_not(None),
            func.trim(source.c.content) != "",
            func.trim(source.c.content).not_in([token.value for token in CustomToken]),
        )

    @staticmethod
    def _free_text_conditions(source):
        """`Turn.is_free_text`, which is a stricter thing than "embeddable".

        A closed answer is still a message and is still embedded as one -- the
        message policy has no survey check -- but a question group made only of
        closed answers is survey scaffolding, and embedding it produces
        near-duplicate vectors that crowd out real answers. So this decides
        which *groups* exist, not which messages do, and the two levels
        deliberately disagree.

        The first message of an interview has no row before it, so its lag is
        NULL: no question, and so no survey item on one.
        """
        return (
            source.c.survey_flag == 0,
            or_(
                source.c.question_survey_flag.is_(None),
                source.c.question_survey_flag == 0,
            ),
        )

    @staticmethod
    def _keyword_condition(filters: EmbeddingFilters, source):
        """The keyword query as a condition on a message row, or None.

        Against the messages rather than the chunk: a chunk's text is a
        rendering built for the model, the question restated included, so
        matching it would let a word in the interviewer's question count as a
        respondent having said it whether or not the reader asked for questions
        to be searched. The two sides are kept apart here -- `content` is what
        the respondent wrote, `question_content` the interviewer turn that drew
        it -- so the scope means something.

        The string is a boolean query, not a literal -- `parse` reads the
        operators and `compile_condition` turns the tree into SQL. A query that
        will not parse raises `KeywordQueryError`; the endpoint turns that into
        a 422 naming the problem, because searching for something other than
        what was typed is worse than refusing.
        """
        node = _keyword_node(filters)
        if node is None:
            return None
        return compile_condition(
            node, source.c.content, source.c.question_content, filters.keyword_scope
        )

    def _keyword_scope(
        self, project_id: UUID, filters: EmbeddingFilters, kind: EmbeddingKind
    ):
        """The keyword, lifted from messages to whichever unit is being scanned.

        A chunk matches when a message inside it does, so the shape of the
        condition follows what the chunk spans: one message, one question group,
        or a whole interview.

        Embeddable rather than free-text messages: whether a unit *exists* is
        the chunk policy's question and is already settled by the time anything
        is scanned. This one only asks whether the unit contains the word.
        """
        source = self._message_source(project_id)
        condition = self._keyword_condition(filters, source)
        if condition is None:
            return None

        matched = select(
            source.c.id,
            source.c.interview_id,
            source.c.section,
            source.c.main_question,
        ).where(*self._embeddable_conditions(source), condition)

        if kind == EmbeddingKind.MESSAGE:
            return EmbeddingTable.message_id.in_(select(matched.subquery().c.id))
        if kind == EmbeddingKind.INTERVIEW:
            return EmbeddingTable.interview_id.in_(
                select(matched.subquery().c.interview_id)
            )

        # A QA pair has no id of its own; it *is* its coordinates.
        group = matched.subquery()
        return (
            select(group.c.id)
            .where(
                group.c.interview_id == EmbeddingTable.interview_id,
                group.c.section == EmbeddingTable.section,
                group.c.main_question == EmbeddingTable.main_question,
            )
            .exists()
        )

    #: Namespace for the synthetic ids of un-embedded browse units. A fixed
    #: UUID so the same chunk keeps the same id across processes and restarts.
    BROWSE_NAMESPACE = UUID("6f2a1c7e-0b3d-4f5a-9c8e-1d2b3a4c5d6e")

    def _interview_scope(self, project_id: UUID, filters: EmbeddingFilters):
        """The interviews in scope, as a select of ids.

        The same conditions `_candidate_statement` applies, expressed once here
        so browsing and scanning cannot drift on what "completed, Danish, not a
        test run" means.
        """
        conditions = [InterviewTable.project_id == project_id]
        if not filters.include_synthetic:
            conditions.append(InterviewTable.type != InterviewType.SYNTHETIC_TEST)
        if filters.status is not None:
            conditions.append(InterviewTable.status == filters.status)
        if filters.participant_id is not None:
            conditions.append(InterviewTable.participant_id == filters.participant_id)
        if filters.created_after is not None:
            conditions.append(InterviewTable.created_at >= filters.created_after)
        if filters.created_before is not None:
            conditions.append(InterviewTable.created_at <= filters.created_before)
        if filters.languages:
            conditions.append(InterviewTable.language.in_(filters.languages))
        if filters.interview_ids is not None:
            conditions.append(InterviewTable.id.in_(filters.interview_ids))
        return select(InterviewTable.id).where(*conditions)

    def browse(
        self,
        *,
        project_id: UUID,
        kind: EmbeddingKind,
        filters: EmbeddingFilters,
        limit: int,
        offset: int,
    ) -> BrowsePage:
        """A page of the corpus in guide order, with no query and no vectors.

        This is the half of the list view that has to work on a project nobody
        has embedded: it reads message rows, groups them into whichever unit was
        asked for, and never touches a vector. Where the corpus *has* been
        embedded the units are matched back to their embedding rows, so a hit
        can still be asked what it is near.

        Ordered by interview and then by position in the guide. A browse has no
        score to rank by, and the alternative to a declared order is a different
        page 2 every time the planner changes its mind.
        """
        source = self._message_source(project_id)

        message_scope = [
            *self._embeddable_conditions(source),
            source.c.interview_id.in_(self._interview_scope(project_id, filters)),
        ]

        keyword = self._keyword_condition(filters, source)
        if keyword is not None:
            message_scope.append(keyword)

        if filters.questions:
            message_scope.append(
                or_(
                    *(
                        and_(
                            source.c.section == section,
                            source.c.main_question == main_question,
                        )
                        for section, main_question in filters.questions
                    )
                )
            )

        # What one row of the listing is, per unit. A MESSAGE is a message; a QA
        # pair is a question group; an interview is an interview. The two
        # grouped kinds additionally require free text somewhere inside them,
        # because that is what makes the group a chunk rather than survey
        # scaffolding -- the distinction a single message is not subject to.
        if kind == EmbeddingKind.MESSAGE:
            grouped = select(
                source.c.interview_id,
                source.c.message_id,
                source.c.id,
                source.c.section,
                source.c.main_question,
                source.c.sub_question,
            ).where(*message_scope)
            order = [source.c.interview_id, source.c.message_id]
        elif kind == EmbeddingKind.QA_PAIR:
            grouped = (
                select(
                    source.c.interview_id,
                    source.c.section,
                    source.c.main_question,
                )
                .where(*message_scope, *self._free_text_conditions(source))
                .group_by(
                    source.c.interview_id,
                    source.c.section,
                    source.c.main_question,
                )
            )
            order = [
                source.c.interview_id,
                source.c.section,
                source.c.main_question,
            ]
        else:
            grouped = (
                select(source.c.interview_id)
                .where(*message_scope, *self._free_text_conditions(source))
                .group_by(source.c.interview_id)
            )
            order = [source.c.interview_id]

        total = self.session.execute(
            select(func.count()).select_from(grouped.subquery())
        ).scalar_one()

        rows = self.session.execute(
            grouped.order_by(*order).limit(limit).offset(offset)
        ).all()

        return BrowsePage(units=self._units_for(project_id, kind, rows), total=total)

    def _units_for(
        self, project_id: UUID, kind: EmbeddingKind, rows
    ) -> list[BrowseUnit]:
        """The page's rows as units, with their interviews and any embeddings.

        Two queries for the whole page rather than two per row: the interviews
        carry the participant a result card names, and the embeddings carry the
        id that makes "more like this" reachable.
        """
        if not rows:
            return []

        interview_ids = {row.interview_id for row in rows}
        interviews = {
            interview.id: interview
            for interview in self.session.execute(
                select(InterviewTable)
                .options(
                    joinedload(InterviewTable.project_participant).joinedload(
                        ProjectParticipantTable.participant
                    )
                )
                .where(InterviewTable.id.in_(interview_ids))
            )
            .unique()
            .scalars()
        }

        # The embedding rows for exactly these units, keyed the way the unit is
        # identified. A miss is the normal state on an un-embedded project and
        # not an error: it costs the row its "more like this", nothing else.
        embeddings = {
            self._unit_key(
                kind,
                row.interview_id,
                row.message_id,
                row.section,
                row.main_question,
            ): row
            for row in self.session.execute(
                select(
                    EmbeddingTable.id,
                    EmbeddingTable.text,
                    EmbeddingTable.interview_id,
                    EmbeddingTable.message_id,
                    EmbeddingTable.section,
                    EmbeddingTable.main_question,
                ).where(
                    EmbeddingTable.project_id == project_id,
                    EmbeddingTable.kind == kind,
                    EmbeddingTable.task == EmbeddingTask.DOCUMENT,
                    EmbeddingTable.interview_id.in_(interview_ids),
                )
            ).all()
        }

        units: list[BrowseUnit] = []
        for row in rows:
            interview = interviews.get(row.interview_id)
            if interview is None:
                continue

            message_id = (
                getattr(row, "id", None) if kind == EmbeddingKind.MESSAGE else None
            )
            section = getattr(row, "section", None)
            main_question = getattr(row, "main_question", None)
            sub_question = getattr(row, "sub_question", None)

            key = self._unit_key(
                kind, row.interview_id, message_id, section, main_question
            )
            embedding = embeddings.get(key)

            units.append(
                BrowseUnit(
                    id=embedding.id if embedding else uuid5(self.BROWSE_NAMESPACE, key),
                    kind=kind,
                    interview_id=row.interview_id,
                    message_id=message_id,
                    section=section,
                    main_question=main_question,
                    sub_question=sub_question,
                    language=interview.language,
                    text=embedding.text if embedding else None,
                    interview=interview,
                    embedded=embedding is not None,
                )
            )

        return units

    @staticmethod
    def _unit_key(
        kind: EmbeddingKind,
        interview_id: UUID,
        message_id: UUID | None,
        section: int | None,
        main_question: int | None,
    ) -> str:
        """What identifies a unit, per kind: a message, a question group, or an
        interview. Deliberately not `chunk_key`, which is built from the chunk
        the embedder produced and so does not exist for a unit nobody embedded.
        """
        if kind == EmbeddingKind.MESSAGE:
            return f"message:{interview_id}:{message_id}"
        if kind == EmbeddingKind.QA_PAIR:
            return f"qa_pair:{interview_id}:{section}:{main_question}"
        return f"interview:{interview_id}"

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

        keyword = self._keyword_scope(project_id, filters, kind)
        if keyword is not None:
            statement = statement.where(keyword)

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
        self,
        embeddings: Sequence[ChunkLike],
        filters: EmbeddingFilters | None = None,
    ) -> dict[UUID, list[EmbeddingTurn]]:
        """The messages behind each chunk, as speaker turns.

        Rendering a result the way the interview read it needs to know who said
        what, and the stored chunk text cannot say: it is one string built for
        the model, and splitting it back on its ``Q:``/``A:`` prefixes is a
        parse of prose that any respondent can break by starting a sentence
        with "Q:". The message rows carry the roles structurally, so they are
        the source here.

        `filters` is taken only for its keyword, so each turn can carry where it
        says what was searched for. The alternative -- letting the client
        re-derive the marks from the query string -- is a second matcher with
        its own idea of what a letter is, marking the text as rendered rather
        than the column the query actually ran against.

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

        node = _keyword_node(filters) if filters else None
        scope: Scope = filters.keyword_scope if filters else "answer"

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
                text = row.content.strip()
                # Against the text as it is rendered, because that is what the
                # offsets index into: the turn is stripped here, so marking the
                # raw column would be off by whatever whitespace it began with.
                side = "answer" if respondent else "question"
                marks = match_spans(text, node, side, scope)
                rendered.append(
                    EmbeddingTurn(
                        role=(
                            TurnRole.RESPONDENT if respondent else TurnRole.INTERVIEWER
                        ),
                        text=text,
                        matches=marks,
                        excluded=excluded_spans(text, node, marks),
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
                # The probe that drew this message, and the message. Nothing
                # else: everything earlier in the group belongs to the *other*
                # messages in it, and a group of three probes rendered as three
                # growing prefixes of one conversation is the same text three
                # times over -- which is what a list of message chunks was.
                #
                # One turn back rather than the whole run of them, because a
                # probe is what a respondent was answering. Where there is no
                # interviewer turn before it -- a respondent writing twice in a
                # row -- the message stands alone rather than borrowing the
                # question of the message above it.
                matched = next(
                    (i for i, turn in enumerate(rendered) if turn.match), None
                )
                if matched is None:
                    continue
                start = matched
                if start > 0 and rendered[start - 1].role == TurnRole.INTERVIEWER:
                    start -= 1
                rendered = rendered[start : matched + 1]

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
