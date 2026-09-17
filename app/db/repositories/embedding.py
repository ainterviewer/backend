"""Storage and search for embedding vectors."""

import hashlib
import logging
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID, uuid5

import numpy as np
from sqlalchemy import (
    Text,
    and_,
    case,
    cast,
    delete,
    distinct,
    false,
    func,
    not_,
    or_,
    select,
    true,
    update,
)
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session, aliased, joinedload

from ainterviewer.interfaces import EmbeddingChunk
from ainterviewer.interview_guides import Image
from ainterviewer.lpm.types import CustomToken
from ainterviewer.types import (
    EmbeddingKind,
    InterviewStatus,
    MessageRole,
    MessageType,
)

from ...types import TurnRole
from ..code_lookup import CodeIndex
from ..keyword_query import (
    MARKUP_PATTERN,
    CodeTerm,
    Scope,
    compile_condition,
    excluded_spans,
    match_spans,
    parse,
    resolve_scope,
    without_code_terms,
)
from ..models import INTERVIEW_PREVIEW_TURNS, ChunkTurns, EmbeddingTurn, TranscriptTurn
from ..survey_answers import SurveyFilter, matching_interviews
from ..tables import (
    CodingTable,
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


#: Whether a chunk must carry a coding, carry none, or may be either.
#:
#: A coverage filter, not a code filter. "none" is the pass that closes a
#: codebook -- what have I not read yet -- and it is why this is not simply a
#: `code:` term with a NOT in front: a negated term is checked per message and
#: then lifted, so a section holding one coded turn and one uncoded one
#: satisfies it. This asks about the chunk.
Coded = Literal["any", "none"]

#: How the two coder axes are joined.
#:
#: They are a 2x2, and the operator is what makes it a complete one: `and`
#: names the four quadrants -- coded by both, only me, to review, read by
#: nobody -- and `or` names their four complements, of which two are questions
#: worth asking. "Coded by anyone" is `any or any`, which is the one reading
#: that is a disjunction and so has no place among the quadrants; "not coded by
#: both" is `none or none`, the work left in a double-coding pass.
#:
#: An axis left unset does not participate, whichever the operator is. If
#: "either" meant *true* under `or` then leaving a row alone would widen the
#: corpus to everything, and the same control would mean opposite things
#: depending on a setting next to it.
CoderJoin = Literal["and", "or"]


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
    #: The two spanning units carry fewer coordinates than this names -- a
    #: SECTION has a section and no question, an INTERVIEW has neither -- so
    #: there the selection is read as containment rather than as equality. See
    #: :meth:`EmbeddingRepository._question_condition`.
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
    #: Which survey answers an interview's respondent must have given, or None
    #: for no survey filter.
    #:
    #: A cohort filter and not a message filter: it keeps every chunk of every
    #: interview whose respondent answered this way, whatever question the chunk
    #: is an answer to. "What did the dissatisfied ones talk about" is the
    #: question it exists for, and picking out the satisfaction answers
    #: themselves would answer a different one.
    survey: SurveyFilter | None = None
    #: Which side of the exchange a bare term is matched against.
    #:
    #: "answer" is the default and the historical behaviour: a chunk restates
    #: the question it answers, so counting a word the interviewer said would
    #: let the guide's own phrasing look like a finding. "question" and "both"
    #: are asked for deliberately, and `q:`/`a:` in the query override this for
    #: a single term.
    keyword_scope: Scope = "answer"
    #: Coverage over the codings `coder_id` made.
    coded_mine: Coded | None = None
    #: Coverage over the codings anybody *but* `coder_id` made.
    coded_others: Coded | None = None
    #: How the two above are joined. See :data:`CoderJoin`.
    coder_join: CoderJoin = "and"
    #: Who the reader is, for the two axes above. Not "whose codings count":
    #: each axis says that for itself.
    #:
    #: Two axes and an operator rather than one coverage filter and a coder,
    #: because the questions a coder asks are about two different scopes at
    #: once and a single scope cannot express them. "What have they coded that
    #: I have not?" -- the second-coder pass -- is `coded_mine="none"` with
    #: `coded_others="any"`, and there is no one scope it is a filter on.
    #:
    #: None of them narrows a `code:` term, which counts anybody's codings.
    #: Scoping one to a coder is a question about the term -- "passages *I*
    #: coded Stress" -- and belongs in the grammar if it is wanted, not in a
    #: filter that would also silently empty the review pass: under
    #: `coded_mine="none"` a coder-scoped `code:` term matches nothing by
    #: construction.
    coder_id: UUID | None = None


def _keyword_node(filters: EmbeddingFilters):
    """The keyword query as a tree, or None where it asks for nothing.

    One place, because the condition that selects rows and the spans that mark
    them have to be reading the same query -- that is the whole reason
    highlighting moved to the server.
    """
    if not filters.keyword or not filters.keyword.strip():
        return None
    return parse(filters.keyword)


def _prose(column):
    """`column` with its tags taken out, for matching against.

    Guide text may carry markup, and a keyword scan run against the raw column
    will match inside it -- `stress*` finds "Stressand" in a support page's
    address. Selecting a row on that returns a result whose evidence a reader
    cannot see, because `match_spans` drops the same span before it is marked.
    Stripping here is what keeps the two answering the same question.

    `regexp_replace` is Postgres's; `app.db.regexp` registers the same
    signature on SQLite, exactly as it does for `REGEXP` itself. Rendered only
    where a question-scoped term actually uses the column, so an answers-only
    search pays nothing for it.
    """
    return func.regexp_replace(column, MARKUP_PATTERN, "", "g")


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
    # How many distinct interviews `total` chunks come from. Counted over the
    # candidate rows for the same reason the totals are: they are already in
    # memory, so it cannot disagree with what was ranked.
    interviews: int = 0


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


#: How a browse is ordered. Not offered for the ranked endpoints, where the
#: score is the order and any other one would throw the answer away.
BrowseOrder = Literal["random", "interview_asc", "interview_desc"]


DEFAULT_BROWSE_ORDER: BrowseOrder = "random"

#: What a page of a browse is blocked into.
#:
#: "interview" keeps one conversation's chunks together and moves the
#: interviews past each other, which is what `order` then decides. "guide"
#: turns the list inside out: every respondent's answer to question 1.1, then
#: every answer to 1.2, so a reader compares one question across people
#: instead of reading people one at a time. `order` still decides whose answer
#: comes first inside each of those blocks.
BrowseGrouping = Literal["interview", "guide"]

DEFAULT_BROWSE_GROUPING: BrowseGrouping = "interview"


@dataclass(frozen=True)
class CodeCoverage:
    """Which chunks in view carry each code, as the units themselves.

    Sets rather than counts: only the caller knows which codes make up a
    branch, and a union is the only way to add two of them up without counting
    a chunk coded with both of them twice.
    """

    #: Chunks in view at all -- what the counts are read against.
    total: int
    #: Code id to the units carrying it, each a tuple of its key columns.
    units: dict[UUID, set[tuple]]


@dataclass(frozen=True)
class BrowsePage:
    """One page of a browse, and how many units it was cut from."""

    units: list[BrowseUnit]
    total: int
    #: How many distinct interviews those units come from.
    interviews: int = 0


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

    def __init__(self, session: Session):
        super().__init__(session)
        # Survey filters resolved during this request, keyed by project and
        # filter. A repository lives for one request, so this never has to be
        # invalidated -- see `_survey_interviews`.
        self._survey_cache: dict[tuple, set[UUID]] = {}
        # The project's codebook, read once if a `code:` term needs it and not
        # at all otherwise. Same lifetime and same reasoning as the cache above.
        self._code_index: dict[UUID, CodeIndex] = {}

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
        previous_id = func.lag(MessageTable.id).over(
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
                # The same row's *id*, on the same condition, so a `q:`-scoped
                # code term can ask whether the question was coded. A UUID is
                # lagged directly where `survey_item` could not be: `lag`
                # returns the raw stored value and skips the column type's
                # decoding, which a UUID does not need and a JSONB did.
                case((previous_is_interviewer == 1, previous_id), else_=None).label(
                    "question_id"
                ),
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

    def _codebook(self, project_id: UUID) -> CodeIndex:
        """The project's codebook, read at most once per request.

        Only ever reached from `_code_condition`, so a query with no `code:` in
        it never asks for it at all.
        """
        if project_id not in self._code_index:
            self._code_index[project_id] = CodeIndex.for_project(
                self.session, project_id
            )
        return self._code_index[project_id]

    @staticmethod
    def _coded_on(
        column,
        ids: tuple[UUID, ...] | None = None,
        *,
        by: UUID | None = None,
        not_by: UUID | None = None,
    ):
        """Whether the message in `column` carries a coding, or one of `ids`.

        Not scoped to the project: `source` is already one project's messages,
        and a coding reaches a message by id.

        `by` narrows to one coder's readings and `not_by` to everybody else's.
        At most one is ever passed -- they are the two halves of one axis -- and
        both left out means anybody's.
        """
        conditions = [CodingTable.message_id == column]
        if ids is not None:
            conditions.append(CodingTable.code_id.in_(ids))
        if by is not None:
            conditions.append(CodingTable.user_id == by)
        if not_by is not None:
            conditions.append(CodingTable.user_id != not_by)
        return select(CodingTable.id).where(*conditions).exists()

    def _coded_anywhere(
        self, source, *, by: UUID | None = None, not_by: UUID | None = None
    ):
        """Whether anything a chunk can reach from this row has been coded.

        The answer, or the question that drew it -- the same two places a code
        term looks, so "uncoded" cannot disagree with `code:`. A section whose
        question somebody marked is a section somebody has read.
        """
        return or_(
            self._coded_on(source.c.id, by=by, not_by=not_by),
            and_(
                source.c.question_id.is_not(None),
                self._coded_on(source.c.question_id, by=by, not_by=not_by),
            ),
        )

    def _lift_to_unit(self, kind: EmbeddingKind, source, condition):
        """A condition on a respondent row, as a condition on this unit's rows.

        A chunk matches when a message inside it does, and the shape of that
        follows what the chunk spans: one message, one question group, one
        section, or a whole interview. Extracted so the keyword scan and the
        coverage filter cannot drift on what "inside" means.
        """
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

        if kind == EmbeddingKind.SECTION:
            # Same shape as the QA pair below, one coordinate shorter: a
            # section chunk carries no `main_question`, so matching on it would
            # compare against NULL and quietly keep nothing.
            inside = matched.subquery()
            return (
                select(inside.c.id)
                .where(
                    inside.c.interview_id == EmbeddingTable.interview_id,
                    inside.c.section == EmbeddingTable.section,
                )
                .exists()
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

    @staticmethod
    def _join(filters: EmbeddingFilters):
        """`and_` or `or_`, per `coder_join`.

        Both are identities over a single operand, so nothing has to special-
        case the one-axis query: the operator is simply inert until there are
        two things for it to join.
        """
        return or_ if filters.coder_join == "or" else and_

    @staticmethod
    def _coverage_axes(filters: EmbeddingFilters):
        """The coverage axes that are asking something, as (setting, scope).

        `scope` is what `_coded_anywhere` takes. A reader with no id has no
        codings of their own, so "mine" becomes a condition nobody satisfies
        and "others" becomes everybody -- which is what the words mean, and
        beats quietly ignoring an axis the caller set.

        An axis left at None is simply absent, which is what makes the operator
        safe: `or` over one operand is that operand, and over none is no filter
        at all, so leaving a row alone never widens the corpus.
        """
        axes = []
        if filters.coded_mine is not None:
            axes.append(
                (filters.coded_mine, {"by": filters.coder_id}, filters.coder_id is None)
            )
        if filters.coded_others is not None:
            axes.append((filters.coded_others, {"not_by": filters.coder_id}, False))
        return axes

    def _coded_scope(
        self, project_id: UUID, filters: EmbeddingFilters, kind: EmbeddingKind
    ):
        """The coverage filter on this unit's rows, or None where it asks nothing.

        The negation sits *outside* the lift, which is the whole point: lifted
        first and negated after asks whether the chunk has any coding at all,
        where negating the message predicate first would ask whether it has any
        uncoded turn -- true of almost every chunk, coded or not.

        `NOT IN` is safe on the two unit kinds that use it: the subquery selects
        a message id and an interview id, neither of which is ever NULL.
        """
        axes = self._coverage_axes(filters)
        if not axes:
            return None

        source = self._message_source(project_id)
        conditions = []
        for setting, scope, nobody in axes:
            if nobody:
                # "Mine" with no reader: nothing is mine, so "any" keeps
                # nothing and "none" keeps everything.
                conditions.append(false() if setting == "any" else true())
                continue
            lifted = self._lift_to_unit(
                kind, source, self._coded_anywhere(source, **scope)
            )
            conditions.append(lifted if setting == "any" else not_(lifted))
        return self._join(filters)(*conditions)

    def _code_condition(
        self, project_id: UUID, filters: EmbeddingFilters, source, term: CodeTerm
    ):
        """One `code:` term as a condition on a message row.

        The same shape as a keyword term, and deliberately so: both end up as a
        predicate over one respondent row, which is what lets `_keyword_scope`
        lift either to whichever unit is being scanned without knowing the
        difference. `code:stress AND kids` is one row that is both.

        The scope reaches the interviewer turn the same way a `q:` term reaches
        its words -- through the lag, not by widening the rows scanned. Widening
        them would change what a *word* means too: a bare term would start
        matching the guide's own phrasing, which is the thing the default scope
        exists to prevent.

        `resolve_scope` decides what an unscoped code term means, rather than
        this deciding it: a bare `code:` is "both", never the caller's default,
        and that rule lives in one place.
        """
        return self._carries_code(
            source,
            self._codebook(project_id).resolve(term),
            resolve_scope(term, filters.keyword_scope),
        )

    def _carries_code(self, source, ids: tuple[UUID, ...], scope: Scope = "both"):
        """Whether a message row carries one of `ids`, on the side `scope` asks.

        The predicate behind both readings of a code: the grammar's `code:`
        term, and the centroid that "find more like these" is built from. One
        expression, so what a code *selects* and what it is *averaged over*
        cannot come apart.
        """
        on_answer = self._coded_on(source.c.id, ids)
        # NULL where the row before was not an interviewer turn, exactly as
        # `question_content` is, so a respondent writing twice running has no
        # question of their own to have been coded.
        on_question = and_(
            source.c.question_id.is_not(None), self._coded_on(source.c.question_id, ids)
        )

        if scope == "answer":
            return on_answer
        if scope == "question":
            return on_question
        return or_(on_answer, on_question)

    def _keyword_condition(
        self,
        project_id: UUID,
        filters: EmbeddingFilters,
        source,
        drop_codes: bool = False,
    ):
        """The keyword query as a condition on a message row, or None.

        `drop_codes` prunes the `code:` leaves out of the tree first, which is
        what the code facets are counted under -- see `without_code_terms`.
        Expressed here rather than by handing in a different query string
        because a tree has no spelling to go back to, and a second parse of a
        second spelling is a second chance for the two to disagree.

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

        Only the question side is stripped of markup, because only guide text
        can contain any: a respondent who types ``<b>`` is shown those
        characters, so a term matching them matched something they can see.
        """
        node = _keyword_node(filters)
        if node is not None and drop_codes:
            node = without_code_terms(node)
        if node is None:
            return None
        return compile_condition(
            node,
            source.c.content,
            _prose(source.c.question_content),
            filters.keyword_scope,
            code=lambda term: self._code_condition(project_id, filters, source, term),
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
        condition = self._keyword_condition(project_id, filters, source)
        if condition is None:
            return None
        return self._lift_to_unit(kind, source, condition)

    #: Namespace for the synthetic ids of un-embedded browse units. A fixed
    #: UUID so the same chunk keeps the same id across processes and restarts.
    BROWSE_NAMESPACE = UUID("6f2a1c7e-0b3d-4f5a-9c8e-1d2b3a4c5d6e")

    def _survey_interviews(
        self, project_id: UUID, filters: EmbeddingFilters
    ) -> set[UUID] | None:
        """The interviews the survey filter allows, or None where it asks for nothing.

        Resolved once per request and remembered: browsing applies the same
        filter in two places, and the map's scan and its count are two more.
        The answer depends only on the project and the filter, so the cache is
        keyed on those and not on which caller asked.
        """
        if not filters.survey:
            return None

        key = (project_id, filters.include_synthetic, filters.survey.key)
        if key not in self._survey_cache:
            self._survey_cache[key] = matching_interviews(
                self.session,
                project_id,
                filters.survey,
                include_synthetic=filters.include_synthetic,
            )
        return self._survey_cache[key]

    def _survey_conditions(self, project_id: UUID, filters: EmbeddingFilters) -> list:
        """The survey filter as conditions on `InterviewTable`, empty for none.

        An empty result is `id IN ()` rather than no condition at all: nobody
        answered that way, and the honest answer to a filter nobody matches is
        no chunks -- not every chunk.
        """
        allowed = self._survey_interviews(project_id, filters)
        if allowed is None:
            return []
        return [InterviewTable.id.in_(sorted(allowed))]

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
        conditions.extend(self._survey_conditions(project_id, filters))
        return select(InterviewTable.id).where(*conditions)

    def _question_condition(
        self, project_id: UUID, kind: EmbeddingKind, filters: EmbeddingFilters
    ):
        """The question filter as a condition on *this* unit's vector rows.

        One selection, read three ways, because the units do not all carry a
        question. A MESSAGE or a QA_PAIR *is* the thing selected and matches its
        coordinates exactly. A SECTION carries a section and no question, so it
        matches when the selection names any question inside it -- the section a
        reader picked a question from is the smallest unit that exists at that
        granularity, and the alternative is an empty view. An INTERVIEW carries
        no coordinates at all and matches when the transcript *contains* an
        answer to one of them, which is the only thing a question can mean about
        a whole conversation: "the interviews where this came up".

        Asking every unit for the exact pair -- which is what this used to do --
        emptied both spanning units outright, so the client compensated by
        throwing the selection away whenever the unit changed. Reading the
        filter against the unit is the same thing said once, in the one place
        that knows which unit is being scanned.

        The browse path expresses this without deciding anything: it filters
        *messages* and then groups them into units, so a section survives when
        one of its messages does. This is that behaviour restated for the rows
        that are already grouped.
        """
        if not filters.questions:
            return None

        if kind == EmbeddingKind.SECTION:
            return EmbeddingTable.section.in_(
                {section for section, _ in filters.questions}
            )

        if kind == EmbeddingKind.INTERVIEW:
            # Scoped by project because message coordinates are only unique
            # within one guide. The outer statement scopes to the project too,
            # so this is belt and braces rather than the only guard.
            return EmbeddingTable.interview_id.in_(
                select(MessageTable.interview_id).where(
                    MessageTable.project_id == project_id,
                    or_(
                        *(
                            and_(
                                MessageTable.section == section,
                                MessageTable.main_question == main_question,
                            )
                            for section, main_question in filters.questions
                        )
                    ),
                )
            )

        # An OR of pairs rather than a row-value `IN`: the list is a handful of
        # questions picked by hand, so the planner sees the same thing either
        # way, and this stays true on every backend rather than only the one
        # that supports row constructors.
        return or_(
            *(
                and_(
                    EmbeddingTable.section == section,
                    EmbeddingTable.main_question == main_question,
                )
                for section, main_question in filters.questions
            )
        )

    def _interview_order(self, column, order: BrowseOrder, seed: str):
        """How the interviews themselves are sequenced, as ORDER BY terms.

        Every order here begins with the interview and only then with the
        guide, so a conversation's chunks stay adjacent whichever is chosen. It
        is the interviews that move.

        `random` hashes the interview id with the caller's seed. Random *by
        interview* rather than by chunk on purpose: the point of shuffling is
        that the reader does not always meet the same people first, and
        scattering one interview's chunks across the mosaic would cost the
        thing the numbering was added for -- seeing a conversation as a
        conversation -- without buying any more independence.

        The interview id is the last term either way, so two interviews that
        hash alike, or that were started in the same instant, cannot swap
        places between two pages of one list.
        """
        if order == "random":
            return [func.md5(cast(column, Text).concat(seed)), column]

        started = (
            select(InterviewTable.created_at)
            .where(InterviewTable.id == column)
            .scalar_subquery()
        )
        if order == "interview_desc":
            return [started.desc(), column.desc()]
        return [started.asc(), column.asc()]

    def interviews_in_scope(
        self, project_id: UUID, filters: EmbeddingFilters
    ) -> set[UUID]:
        """The interviews the filters leave, as ids.

        The same scope the corpus is cut to, resolved rather than composed into
        a larger query, so that something which counts *interviews* -- the
        cohort filter's own tallies -- can be counted over exactly the
        interviews the reader is looking at.
        """
        return set(
            self.session.execute(self._interview_scope(project_id, filters)).scalars()
        )

    def _browse_scope(
        self,
        project_id: UUID,
        filters: EmbeddingFilters,
        kind: EmbeddingKind,
        drop_codes: bool = False,
    ):
        """What the filters leave, as conditions on message rows and on groups.

        Returns the message source, the conditions every row must satisfy, and
        the conditions every *group* must satisfy. Shared between browsing and
        the code facets because a badge that counted over a different corpus
        than the list it sits beside would be a number nobody could act on.
        """
        source = self._message_source(project_id)

        message_scope = [
            *self._embeddable_conditions(source),
            source.c.interview_id.in_(self._interview_scope(project_id, filters)),
        ]

        keyword = self._keyword_condition(project_id, filters, source, drop_codes)
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

        # The coverage filter, asked of the chunk rather than of a message.
        #
        # A MESSAGE chunk *is* the row, so each axis is an ordinary condition.
        # The grouped kinds have to wait for the group: "this section has no
        # coding" is a fact about every row in it at once, which is what HAVING
        # is for. Filtering rows first would ask whether the section has an
        # uncoded turn, which nearly every section does.
        #
        # Joined once at the end rather than appended one axis at a time: under
        # `or` the two are a single condition, and two conditions in the same
        # list are an `and` whatever the operator says.
        coded_rows = []
        coded_groups = []
        for setting, scope, nobody in self._coverage_axes(filters):
            if nobody:
                # "Mine" with no reader -- see `_coverage_axes`.
                coded_rows.append(false() if setting == "any" else true())
                coded_groups.append(false() if setting == "any" else true())
                continue
            anywhere = self._coded_anywhere(source, **scope)
            coded_rows.append(anywhere if setting == "any" else not_(anywhere))
            marked = func.max(case((anywhere, 1), else_=0))
            coded_groups.append(marked == 1 if setting == "any" else marked == 0)

        coded_having = []
        if coded_rows:
            join = self._join(filters)
            if kind == EmbeddingKind.MESSAGE:
                message_scope.append(join(*coded_rows))
            else:
                coded_having.append(join(*coded_groups))

        return source, message_scope, coded_having

    def browse(
        self,
        *,
        project_id: UUID,
        kind: EmbeddingKind,
        filters: EmbeddingFilters,
        limit: int,
        offset: int,
        order: BrowseOrder = DEFAULT_BROWSE_ORDER,
        seed: str = "",
        group_by: BrowseGrouping = DEFAULT_BROWSE_GROUPING,
    ) -> BrowsePage:
        """A page of the corpus in guide order, with no query and no vectors.

        This is the half of the list view that has to work on a project nobody
        has embedded: it reads message rows, groups them into whichever unit was
        asked for, and never touches a vector. Where the corpus *has* been
        embedded the units are matched back to their embedding rows, so a hit
        can still be asked what it is near.

        Ordered by interview and then by position in the guide -- which
        interview comes first is what `order` decides, and `seed` is what makes
        a shuffle hold still across the pages of one list. A browse has no score
        to rank by, and the alternative to a declared order is a different page
        2 every time the planner changes its mind.

        `group_by` chooses which of the two axes blocks the page. Grouping by
        interview reads the corpus a conversation at a time; grouping by the
        guide reads it a question at a time, which is the shape most
        cross-interview comparison wants. It is an ordering and nothing else --
        no row leaves, and `total` is the same number either way. Meaningless
        under the INTERVIEW unit, which carries no guide coordinates to block
        by, and there it falls back to interview order.
        """
        source, message_scope, coded_having = self._browse_scope(
            project_id, filters, kind
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
            within = [source.c.message_id]
            guide_terms = [
                source.c.section,
                source.c.main_question,
                source.c.sub_question,
            ]
        elif kind == EmbeddingKind.SECTION:
            # One row per section that drew free text anywhere in it. The
            # free-text test is the same one a question group is held to, one
            # level up: a section of nothing but closed answers is structured
            # data, and letting it through here would be the survey answers
            # walking back in under a different unit.
            grouped = (
                select(source.c.interview_id, source.c.section)
                .where(*message_scope, *self._free_text_conditions(source))
                .group_by(source.c.interview_id, source.c.section)
            )
            if coded_having:
                grouped = grouped.having(and_(*coded_having))
            within = [source.c.section]
            guide_terms = [source.c.section]
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
            if coded_having:
                grouped = grouped.having(and_(*coded_having))
            within = [source.c.section, source.c.main_question]
            guide_terms = [source.c.section, source.c.main_question]
        else:
            grouped = (
                select(source.c.interview_id)
                .where(*message_scope, *self._free_text_conditions(source))
                .group_by(source.c.interview_id)
            )
            if coded_having:
                grouped = grouped.having(and_(*coded_having))
            # An interview spans the guide, so there is no coordinate to block
            # a page of them by.
            within = []
            guide_terms = []

        counted = grouped.subquery()
        total, interviews = self.session.execute(
            select(
                func.count(), func.count(distinct(counted.c.interview_id))
            ).select_from(counted)
        ).one()

        # Guide grouping puts the coordinates ahead of the interview terms, so
        # the page runs question by question with the interviews shuffled or
        # dated inside each. `within` still trails both, which is what keeps one
        # unit's rows in one place under either grouping.
        ordering = [
            *(guide_terms if group_by == "guide" else ()),
            *self._interview_order(source.c.interview_id, order, seed),
            *within,
        ]

        rows = self.session.execute(
            grouped.order_by(*ordering).limit(limit).offset(offset)
        ).all()

        return BrowsePage(
            units=self._units_for(project_id, kind, rows),
            total=total,
            interviews=interviews,
        )

    @staticmethod
    def _unit_key_columns(kind: EmbeddingKind, source) -> list:
        """The columns that say which unit a message row belongs to.

        The same keys `browse` groups by: a MESSAGE is its own row, a section
        and a question group are coordinates inside an interview, and an
        interview is itself.
        """
        if kind == EmbeddingKind.MESSAGE:
            return [source.c.id]
        if kind == EmbeddingKind.SECTION:
            return [source.c.interview_id, source.c.section]
        if kind == EmbeddingKind.QA_PAIR:
            return [source.c.interview_id, source.c.section, source.c.main_question]
        return [source.c.interview_id]

    def code_coverage(
        self, *, project_id: UUID, kind: EmbeddingKind, filters: EmbeddingFilters
    ) -> CodeCoverage:
        """Which chunks now in view carry each code.

        The units themselves rather than their counts, because a branch's total
        is the *union* of its codes' chunks: a chunk carrying both a parent and
        its child is one chunk, and summing the rows down the branch would
        report it twice. Small enough to hold -- a pair per coding, not per
        chunk.

        `filters` should already have its code terms pruned
        (`without_code_terms`). Counted with them, choosing one code would take
        every other badge to zero and leave the reader inside a selection they
        can no longer see out of -- the rule `_facet_cohorts` keeps for the
        survey items, for the same reason.

        A coding on the interviewer's turn counts for the chunk that answers
        it, which is what a bare `code:x` reaches: the term's default scope is
        both sides, so a badge that counted only answers would offer a number
        the filter then beat.
        """
        source, message_scope, coded_having = self._browse_scope(
            project_id, filters, kind, drop_codes=True
        )
        key = self._unit_key_columns(kind, source)
        grouped = kind != EmbeddingKind.MESSAGE
        # What makes a *group* a chunk rather than survey scaffolding. Applied
        # to the coded rows as well as to the units, because `browse` applies
        # it alongside the keyword: a coding sitting on a closed answer is not
        # reachable by `code:` under a grouped unit either, and a badge that
        # counted it would send the reader somewhere empty.
        free_text = list(self._free_text_conditions(source)) if grouped else []

        units = select(*key).where(*message_scope, *free_text)
        if grouped:
            units = units.group_by(*key)
            if coded_having:
                units = units.having(and_(*coded_having))
        in_view = units.subquery()

        total = self.session.execute(
            select(func.count()).select_from(in_view)
        ).scalar_one()

        # Aliased, and it has to be: the coverage axes in `message_scope` are
        # EXISTS subqueries over the same table, and with `CodingTable` itself
        # in the enclosing FROM they would auto-correlate to it and lose their
        # own FROM clause entirely.
        coding = aliased(CodingTable)
        on_coding = or_(
            coding.message_id == source.c.id,
            and_(
                source.c.question_id.is_not(None),
                coding.message_id == source.c.question_id,
            ),
        )
        pairs = (
            select(coding.code_id, *key)
            .select_from(
                source.join(coding, on_coding).join(
                    in_view,
                    and_(*(column == in_view.c[column.name] for column in key)),
                )
            )
            .where(*message_scope, *free_text)
            .distinct()
        )

        units_by_code: dict[UUID, set[tuple]] = {}
        for row in self.session.execute(pairs):
            units_by_code.setdefault(row[0], set()).add(tuple(row[1:]))
        return CodeCoverage(total=total, units=units_by_code)

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
        if kind == EmbeddingKind.SECTION:
            return f"section:{interview_id}:{section}"
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

        questions = self._question_condition(project_id, kind, filters)
        if questions is not None:
            statement = statement.where(questions)

        coded = self._coded_scope(project_id, filters, kind)
        if coded is not None:
            statement = statement.where(coded)

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

        interview_conditions.extend(self._survey_conditions(project_id, filters))

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

    def interview_numbers(self, project_id: UUID) -> dict[UUID, int]:
        """Each interview's position in its project, counting from one.

        Numbered over every interview the project has, not over the ones a
        request left standing: the number exists so a reader can say "these two
        cards are the same interview" and have that hold across searches, and a
        rank computed inside a filtered set would renumber itself whenever the
        filters moved.

        Ordered by when the interview was started, with the id breaking ties so
        two interviews created in the same instant cannot swap numbers between
        requests. One row per interview rather than a count per hit, because the
        page needs the numbers of at most a few dozen and the alternative is a
        correlated subquery on every one of them.
        """
        rows = self.session.execute(
            select(
                InterviewTable.id,
                func.row_number().over(
                    order_by=(InterviewTable.created_at, InterviewTable.id)
                ),
            ).where(InterviewTable.project_id == project_id)
        ).all()
        return {row[0]: row[1] for row in rows}

    def turns_for(
        self,
        embeddings: Sequence[ChunkLike],
        filters: EmbeddingFilters | None = None,
        whole_interviews: bool = False,
    ) -> dict[UUID, ChunkTurns]:
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

        A SECTION gets its turns whole: it is the unit that exists to be read
        that way, since the closed answer at the top of a section is the context
        for the open questions under it, and a section runs to a few hundred
        characters rather than a transcript's several thousand.

        An INTERVIEW gets a window rather than the whole transcript -- shipping
        one transcript per hit would dwarf the rest of the response, and ten of
        them stacked is not a list anybody can scan. The window is
        `INTERVIEW_PREVIEW_TURNS` long and opens on the first keyword match
        where there is one, on the first turn otherwise: a reader who arrived
        from a search is here to see the search, and a preview that always
        showed the opening pleasantries would show the one part of an interview
        that is the same in all of them. `ChunkTurns.total` carries the full
        length either way, so the card can say what it is not showing.

        `whole_interviews` turns that window off, for the one caller that wants
        the transcripts themselves: a list browsing the INTERVIEW unit is a list
        *of* interviews, and a six-turn window onto each is a list of openings.
        It costs a transcript per hit on the wire, which is why it is asked for
        rather than assumed -- a page of cluster representatives still wants the
        window.

        One query for every hit on the page, grouped in Python: the alternative
        is a query per chunk, and a page of ten results with three
        representatives per cluster makes that dozens of round trips.
        """
        wanted = [
            embedding
            for embedding in embeddings
            if embedding.kind
            in (
                EmbeddingKind.QA_PAIR,
                EmbeddingKind.MESSAGE,
                EmbeddingKind.SECTION,
                EmbeddingKind.INTERVIEW,
            )
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
                MessageTable.sub_question,
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

        # (interview, section, main_question) -> the group's messages, in order,
        # (interview, section) -> the whole section's, and interview -> all of
        # them.
        #
        # Three indexes over one pass because the kinds sit at three levels: a
        # QA pair *is* a question group and a message is one turn inside one,
        # a section spans the groups -- which is the whole reason it exists,
        # since the closed answer it opens with is a group of its own -- and an
        # interview spans the sections.
        grouped: dict[tuple[UUID, int, int], list[Any]] = {}
        sections: dict[tuple[UUID, int], list[Any]] = {}
        interviews: dict[UUID, list[Any]] = {}
        for row in rows:
            if row.skipped_by_condition or not row.content.strip():
                continue
            if row.content.strip() in CustomToken:
                continue
            grouped.setdefault(
                (row.interview_id, row.section, row.main_question), []
            ).append(row)
            sections.setdefault((row.interview_id, row.section), []).append(row)
            interviews.setdefault(row.interview_id, []).append(row)

        node = _keyword_node(filters) if filters else None
        scope: Scope = filters.keyword_scope if filters else "answer"

        turns: dict[UUID, ChunkTurns] = {}
        for embedding in wanted:
            # Checked before the coordinates are read, not after: an INTERVIEW
            # chunk spans the whole guide and so carries none of them.
            if embedding.kind == EmbeddingKind.INTERVIEW:
                group = interviews.get(embedding.interview_id)
            elif embedding.section is None:
                continue
            elif embedding.kind == EmbeddingKind.SECTION:
                group = sections.get((embedding.interview_id, embedding.section))
            elif embedding.main_question is None:
                continue
            else:
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
                        id=row.id,
                        role=(
                            TurnRole.RESPONDENT if respondent else TurnRole.INTERVIEWER
                        ),
                        text=text,
                        matches=marks,
                        excluded=excluded_spans(text, node, marks, side),
                        survey_label=(item or pending_item) if respondent else None,
                        # Only a MESSAGE chunk singles a turn out; for a QA pair
                        # the whole group is the chunk.
                        match=(
                            embedding.kind == EmbeddingKind.MESSAGE
                            and row.id == embedding.message_id
                        ),
                        # Its own place in the guide, not the group's. Every
                        # turn here shares the group's section and question by
                        # construction, but the probes under it are what a
                        # reader is telling apart -- `3.2.1` from `3.2.2` --
                        # and that is the sub-question.
                        section=row.section,
                        main_question=row.main_question,
                        sub_question=row.sub_question,
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

            total = len(rendered)

            if embedding.kind == EmbeddingKind.INTERVIEW and not whole_interviews:
                # A window onto the conversation, opened where the reader's
                # attention already is. The first marked turn where the search
                # was a keyword one, backing up to the question that drew it so
                # a marked answer is never shown without what it answers;
                # otherwise the top, which is all "the start of this interview"
                # can mean.
                hit = next((i for i, turn in enumerate(rendered) if turn.matches), None)
                start = hit if hit is not None else 0
                if start > 0 and rendered[start - 1].role == TurnRole.INTERVIEWER:
                    start -= 1
                rendered = rendered[start : start + INTERVIEW_PREVIEW_TURNS]

            turns[embedding.id] = ChunkTurns(turns=rendered, total=total)

        return turns

    def transcript(
        self,
        project_id: UUID,
        interview_id: UUID,
        keyword: str | None = None,
        keyword_scope: Scope = "answer",
    ) -> list[TranscriptTurn]:
        """One interview, whole, as speaker turns with the keyword marked.

        `turns_for` renders the messages behind a *chunk*; this renders the
        messages behind an interview, which is the same rows read without the
        grouping. Kept apart rather than generalised because the two differ in
        what they leave out: a chunk keeps only its own question group and only what
        a card has room for, while a transcript keeps everything -- turns said
        before the first question, questions the guide skipped, the control
        tokens between sections, and each survey item in full. A card is a
        summary and can afford to drop those; a transcript is the record.

        Scoped by `project_id` as well as `interview_id`. The caller has been
        authorised for the project, not for the interview, so an interview
        belonging to another project has to read as absent rather than as
        forbidden.

        Raises `NoResultFound` where the interview is not this project's.
        """
        exists = self.session.execute(
            select(InterviewTable.id).where(
                InterviewTable.id == interview_id,
                InterviewTable.project_id == project_id,
            )
        ).first()
        if exists is None:
            raise NoResultFound(f"Interview {interview_id} not found in this project")

        rows = self.session.execute(
            select(
                MessageTable.id,
                MessageTable.role,
                MessageTable.content,
                MessageTable.section,
                MessageTable.main_question,
                MessageTable.sub_question,
                MessageTable.survey_item,
                MessageTable.skipped_by_condition,
                MessageTable.image,
            )
            .where(
                MessageTable.interview_id == interview_id,
                MessageTable.role != MessageRole.SYSTEM,
            )
            .order_by(MessageTable.message_id)
        ).all()

        node = parse(keyword) if keyword and keyword.strip() else None

        turns: list[TranscriptTurn] = []
        # The item is stored on the interviewer's message and read on the
        # respondent's, because what a reader judges is the answer and the
        # options it was chosen from together. The transcript page moves it the
        # same way; doing it here means both do it once.
        pending_item = None
        for row in rows:
            if not row.content.strip():
                continue

            respondent = MessageRole(row.role) == MessageRole.USER
            item = row.survey_item
            text = row.content.strip()
            # Marked against the stripped text, because that is what the
            # offsets index into -- the same reason `turns_for` strips first.
            side = "answer" if respondent else "question"
            marks = match_spans(text, node, side, keyword_scope)
            carried = item or pending_item if respondent else None
            image = row.image if isinstance(row.image, Image) else None
            turns.append(
                TranscriptTurn(
                    id=row.id,
                    role=TurnRole.RESPONDENT if respondent else TurnRole.INTERVIEWER,
                    text=text,
                    matches=marks,
                    excluded=excluded_spans(text, node, marks, side),
                    survey_label=carried.type if carried else None,
                    survey_item=carried,
                    image=image,
                    skipped=row.skipped_by_condition,
                    section=row.section,
                    main_question=row.main_question,
                    sub_question=row.sub_question,
                )
            )
            pending_item = None if respondent else item

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
        exclude: Collection[UUID] = (),
    ) -> EmbeddingSearchPage:
        """Score every candidate, return one page of the ranking.

        Paging re-scores the whole candidate set on every request. That is the
        same work the first page does -- one matrix product over a few thousand
        rows, single-digit milliseconds -- and it keeps a page a pure function
        of the query and the corpus, with no ranking to cache, invalidate or
        pin to a session.
        """
        scored = len(rows)
        if exclude:
            rows = [row for row in rows if row[0] not in exclude]
        # The candidate rows carry their interview, so the spread of the
        # ranking is a set over rows already read rather than a second query.
        interviews = len({row[2] for row in rows})
        if not rows or offset >= len(rows):
            return EmbeddingSearchPage(
                hits=[], scored=scored, total=len(rows), interviews=interviews
            )

        query = np.asarray(query_vector, dtype=VECTOR_DTYPE)
        matrix = np.frombuffer(
            b"".join(row[1] for row in rows), dtype=VECTOR_DTYPE
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
            interviews=interviews,
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
            ).add_columns(EmbeddingTable.interview_id)
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
            ).add_columns(EmbeddingTable.interview_id)
        ).all()

        return source, self._rank(
            rows, decode_vector(source.vector), limit, offset, exclude={source.id}
        )

    def like_code(
        self,
        *,
        project_id: UUID,
        code_ids: tuple[UUID, ...],
        kind: EmbeddingKind = EmbeddingKind.QA_PAIR,
        task: EmbeddingTask = EmbeddingTask.DOCUMENT,
        limit: int = 10,
        offset: int = 0,
        filters: EmbeddingFilters | None = None,
    ) -> tuple[int, EmbeddingSearchPage]:
        """The corpus ranked against the average of what a code sits on.

        "More like this" with a code for the this. The seed set is exactly what
        a bare `code:` term selects -- both sides of the exchange, through the
        same `_carries_code` predicate -- so the passages this is built from
        are the passages the filter would have shown.

        Costs no inference, like `similar_to`: every vector it needs is already
        stored, so this works with the embedding server down.

        The seeds stay in the ranking. Dropping them would be this method
        deciding what the reader meant -- and taking away the one thing the
        ranking says about the code itself, which is whether the passages it
        was built from actually sit together. A seed ranking low is a seed
        somebody coded loosely, and that is worth seeing.

        Narrowing to where the code has *not* reached is a filter, and the
        grammar already has it: `-code:x` in the keyword box, which composes
        with everything else and says exactly which code it means. A flag here
        would be a second way to say the same thing.

        Deliberately not filtered to the seeds' coders or to a subtree
        decision: `code_ids` is whatever the caller resolved, which is one code
        or a branch, and the caller is the one that knows which was asked for.
        """
        source = self._message_source(project_id)
        seeds = self.session.execute(
            select(EmbeddingTable.id, EmbeddingTable.vector).where(
                EmbeddingTable.project_id == project_id,
                EmbeddingTable.kind == kind,
                EmbeddingTable.task == task,
                self._lift_to_unit(kind, source, self._carries_code(source, code_ids)),
            )
        ).all()

        if not seeds:
            # Refused rather than answered with the corpus in arbitrary order:
            # a centroid of nothing is not a query, and ranking against a zero
            # vector would return everything at a score of 0 and look like an
            # answer.
            #
            # Which of the two reasons it is matters to the reader, and the
            # count they are looking at cannot tell them. A badge counts what
            # the *filter* reaches, and browsing works on a project nobody has
            # embedded -- so a code can read 1 beside an action that has no
            # vector to average. Saying "nothing has been coded with this"
            # there would be a plain lie.
            carried = self.session.execute(
                select(func.count())
                .select_from(source)
                .where(
                    *self._embeddable_conditions(source),
                    self._carries_code(source, code_ids),
                )
            ).scalar_one()
            if carried:
                raise ValueError(
                    "The passages coded with this have not been embedded yet, "
                    "so there is nothing to average — they are still findable "
                    "by filtering to the code."
                )
            raise ValueError(
                "Nothing has been coded with this yet, so there is nothing to "
                "be like — search by the code's definition instead."
            )

        stacked = np.frombuffer(
            b"".join(row[1] for row in seeds), dtype=VECTOR_DTYPE
        ).reshape(len(seeds), -1)
        centroid = stacked.mean(axis=0)
        # Back to unit length, because the stored vectors are and the scores are
        # read as cosines. The mean of unit vectors is shorter than one -- the
        # more the seeds disagree, the shorter -- so leaving it would scale
        # every score by how incoherent the code is and show that as relevance.
        norm = float(np.linalg.norm(centroid))
        if norm > 0:
            centroid = centroid / norm

        rows = self.session.execute(
            self._candidate_statement(
                project_id=project_id,
                kind=kind,
                task=task,
                filters=filters or EmbeddingFilters(),
            ).add_columns(EmbeddingTable.interview_id)
        ).all()

        return len(seeds), self._rank(rows, centroid, limit, offset)

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
