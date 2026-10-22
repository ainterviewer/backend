"""Semantic search, browsing, facets and clustering over the corpus."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import UUID

from pydantic import UUID4, Field, model_validator

from ainterviewer.interview_guides import Image, SurveyItem
from ainterviewer.types import EmbeddingKind, InterviewStatus, LanguageCode

from ..db.keyword_query import Scope
from ..types import GroupKind, InterviewType, Projection, TurnRole
from .common import LanguageFilter, Schema

if TYPE_CHECKING:
    from ..db.tables import InterviewTable


class EmbeddingTurn(Schema):
    """One speaker turn inside a chunk, as it was said in the interview.

    A chunk's stored `text` is the rendering the *model* saw -- one string with
    ``Q:``/``A:`` prefixes -- and re-splitting it on those prefixes would be a
    parse of prose that a respondent can break by starting a sentence with
    "Q:". These come from the message rows instead, so the roles are structural
    and a result reads the way the conversation did.
    """

    #: The message row this turn is, so a client can act on it.
    #:
    #: On the base model rather than only on `TranscriptTurn` because a turn is
    #: the unit a reader codes, and explore draws turns everywhere -- inside a
    #: QA-pair or section card as much as in a transcript. A hit's own
    #: `message_id` is set for MESSAGE chunks alone, so without this there is
    #: nothing to hang a coding on anywhere else, and coding from the results
    #: list would be possible at one unit out of four.
    id: UUID
    role: TurnRole
    text: str
    # The survey item type, when the answer was a chosen option rather than
    # written text. The distinction matters to a reader: an identical "Agree"
    # from forty respondents is a click, not a consensus.
    survey_label: str | None = None
    # The one turn a MESSAGE hit is actually about; its neighbours are context.
    match: bool = False
    # Where in `text` the keyword query matched, as `(start, end)` character
    # offsets into this string, already merged and in order.
    #
    # Reported rather than left to the client because the scope decides it: with
    # the search set to answers, the same word in the interviewer's question is
    # not a match, and only the side that ran the query knows that. Offsets and
    # not marked-up text -- the client escapes what it renders.
    matches: list[tuple[int, int]] = Field(default_factory=list)
    # Where in `text` a term the query *excluded* appears anyway, same offsets.
    #
    # A negated term can survive on screen two ways: in text the scope never
    # searched, or in a sibling turn of a grouped chunk, since the condition is
    # checked per message and then lifted to the group. Reported separately so a
    # reader can see why a result looks like it contradicts its own query.
    excluded: list[tuple[int, int]] = Field(default_factory=list)
    # Where in the guide this turn was said, so a message can be numbered the
    # way the transcript numbers it -- `3.2` for a main question, `3.2.1` for
    # the first probe under it.
    #
    # Carried per turn rather than read off the chunk, even though a chunk's
    # turns all sit under one question by construction: the probes inside a
    # question group are exactly what differ, and a card that stamped the
    # group's number on all of them would say `3.2` three times where the
    # transcript says `3.2.1`, `3.2.2`, `3.2.3`. An INTERVIEW chunk spans the
    # whole guide and has no number of its own at all.
    section: int | None = None
    main_question: int | None = None
    sub_question: int | None = None


class TranscriptTurn(EmbeddingTurn):
    """One turn of a whole interview, for reading a hit in its context.

    An `EmbeddingTurn` with everything a chunk has no room for: the survey item
    in full, the image, and whether the guide skipped past it. The message id
    and the coordinates it is scrolled to are the base model's, since a card
    identifies and numbers its turns from them too.

    Inherits `matches`/`excluded` rather than restating them, so a transcript
    renders through the same component a chunk does -- the search is still
    marked once the reader has left the mosaic, which is most of the point of
    reading the transcript at all.
    """

    #: The survey item in full, where the answer was a chosen option.
    #:
    #: `EmbeddingTurn.survey_label` carries only the item's *type*, which is all
    #: a results card has room for. A transcript is read to judge an answer, and
    #: an option cannot be judged apart from the options it was chosen from --
    #: "Rarely" means nothing until you can see it was picked over "Never".
    #:
    #: Carried on the answer rather than on the question that posed it, the same
    #: move the transcript page makes: the item belongs to the interviewer's
    #: message in the database and to the respondent's on screen.
    survey_item: SurveyItem | None = None
    #: Asked but never reached, because a condition routed around it. Kept
    #: rather than dropped: a question the guide skipped is part of how the
    #: interview went, and the transcript page has always shown it faded.
    skipped: bool = False
    #: The image attached to the message, where there was one.
    image: Image | None = None


class InterviewTranscript(Schema):
    """A whole interview as turns, marked with what the keyword query found.

    Separate from the messages endpoint the transcript *page* loads, which
    serves annotations and comments and knows nothing about a keyword query.
    This one exists so the explore view can open a transcript without a
    navigation, and it answers only that: read it, see the search in it, go
    back. Annotating is still the page's.
    """

    interview_id: UUID
    turns: list[TranscriptTurn]


#: How much of an interview-level chunk a result card is given.
#:
#: An INTERVIEW chunk spans a whole transcript, and a page of ten of them
#: rendered whole is not a list anybody can scan -- it is ten transcripts, and
#: the reader already has a transcript view a click away. So the card gets a
#: window onto the conversation rather than the conversation: enough turns to
#: hear what kind of interview this is, with `ChunkTurns.total` saying how much
#: was left behind so the card can be honest about being a window.
#:
#: Six is three exchanges, which is where an interview stops sounding like its
#: opening pleasantries and starts sounding like itself.
INTERVIEW_PREVIEW_TURNS = 6


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


class HitSource(ChunkLike, Protocol):
    """What `EmbeddingSearchHit.from_hit` reads: a chunk's coordinates, its
    text and the interview it came from.

    Answered by an `EmbeddingTable` row and by a `BrowseUnit` alike. A unit
    also says whether it was `embedded`; a row always was, so that one is read
    with a default rather than demanded here.
    """

    @property
    def sub_question(self) -> int | None: ...
    @property
    def language(self) -> str: ...
    @property
    def text(self) -> str | None: ...
    @property
    def interview(self) -> InterviewTable | None: ...


@dataclass(frozen=True)
class ChunkTurns:
    """The turns a card draws for one chunk, and how many the chunk holds.

    The two are the same number for every kind but INTERVIEW, which is windowed
    -- see `INTERVIEW_PREVIEW_TURNS`. Returned as a pair rather than as a bare
    list so that a truncated card can say so: "6 of 47 turns" is a window, while
    six turns with no count is a claim that the interview was six turns long.
    """

    turns: list[EmbeddingTurn]
    total: int


class EmbeddingSearchHit(Schema):
    """One semantic-search result, renderable on its own.

    Carries the matched text and the interview context around it, because the
    alternative is a request per hit: a QA-pair chunk spans several messages and
    has no `message_id` to fetch, so there is nothing a client could resolve it
    to. `message_id` is set for MESSAGE hits only, and is the handle for the
    existing annotation, comment and message-context endpoints.
    """

    # `UUID` rather than `UUID4`: a browsed unit that was never embedded has no
    # row of its own, so its id is derived from its coordinates with `uuid5` --
    # deterministic, so the same chunk keeps the same id across processes, and
    # therefore version 5. Demanding version 4 here rejected exactly the rows
    # this endpoint exists to serve.
    id: UUID
    # Cosine similarity in [-1, 1]; vectors are L2-normalised, so this is a
    # plain dot product. Exposed so a client can show ranking confidence and
    # cut off weak matches, which a bare ordering cannot support.
    #
    # None on a browsed row, which was not ranked against anything: a browse is
    # the corpus in guide order, and inventing a score for it would invite a
    # reader to compare numbers that mean nothing.
    score: float | None = None
    # Whether this unit has a stored vector. False only when browsing a corpus
    # that was never embedded -- the row still reads, but there is nothing to
    # ask it what it is near, so a client hides "more like this" rather than
    # offering a button that 404s.
    embedded: bool = True
    kind: EmbeddingKind
    text: str | None

    interview_id: UUID4
    message_id: UUID4 | None
    section: int | None
    main_question: int | None
    sub_question: int | None
    language: LanguageCode

    # Interview context, so a result row can say when and from whom without a
    # follow-up request.
    interview_created_at: datetime | None = None
    interview_status: InterviewStatus | None = None
    interview_type: InterviewType | None = None
    participant_id: UUID4 | None = None
    participant_pid: str | None = None
    # Which interview this is within its project, counting from one in the
    # order they were started.
    #
    # A number rather than the id, because the reader's question is "are these
    # two cards the same person" and a UUID cannot be held in the eye long
    # enough to answer it. Numbered over the whole project rather than over the
    # result set, so it means the same thing in every search, in the transcript
    # it opens, and tomorrow -- a number that renumbered itself per query would
    # be worse than none, since it would look like it identified something.
    #
    # Not a substitute for `participant_pid`: a pid identifies a person across
    # projects and is often absent, while this identifies an interview within
    # one project and is always there.
    interview_number: int | None = None

    # The chunk as a conversation, when the messages behind it could be found.
    # Empty is a normal state, not an error -- an interview whose message rows
    # no longer line up with the chunk's coordinates still has its `text`, and
    # a client renders that instead.
    turns: list[EmbeddingTurn] = []

    # How many turns the unit actually holds, when its messages could be found.
    #
    # The same as `len(turns)` for every kind but INTERVIEW, whose card gets a
    # window onto the transcript rather than the transcript. A client draws the
    # difference -- "6 of 47 turns" -- so that a windowed card reads as a window
    # and not as a short interview. None where there were no messages to count,
    # which is the same state as an empty `turns`.
    n_turns: int | None = None

    @classmethod
    def from_hit(
        cls,
        embedding: HitSource,
        score: float | None,
        turns: ChunkTurns | None = None,
        number: int | None = None,
    ) -> EmbeddingSearchHit:
        """One row, from either an embedding or a browsed unit.

        `BrowseUnit` is shaped to answer the same attributes deliberately, so
        browsing renders through this rather than through a parallel model that
        would drift from it card by card.
        """
        interview = embedding.interview
        project_participant = interview.project_participant if interview else None
        participant = project_participant.participant if project_participant else None

        return cls(
            id=embedding.id,
            score=score,
            embedded=getattr(embedding, "embedded", True),
            kind=embedding.kind,
            text=embedding.text,
            interview_id=embedding.interview_id,
            message_id=embedding.message_id,
            section=embedding.section,
            main_question=embedding.main_question,
            sub_question=embedding.sub_question,
            language=embedding.language,
            interview_created_at=interview.created_at if interview else None,
            interview_status=interview.status if interview else None,
            interview_type=interview.type if interview else None,
            participant_id=project_participant.id if project_participant else None,
            participant_pid=participant.pid if participant else None,
            interview_number=number,
            turns=turns.turns if turns else [],
            n_turns=turns.total if turns else None,
        )


class EmbeddingPage(Schema):
    """One page of chunks, and what the whole result counts."""

    #: How many chunks the page walks through: the length of the ranking on
    #: the ranked endpoints, the chunks in view when browsing.
    total: int = 0
    #: How many distinct interviews the counted chunks come from.
    #:
    #: Counted over everything that matched rather than over the page, which is
    #: the only version that holds still: a client counting the interviews in
    #: what it has loaded would show a number that climbs with every "Load
    #: more", and a reader would read that as the corpus growing.
    interviews: int = 0
    offset: int = 0
    items: list[EmbeddingSearchHit] = []


class RankedEmbeddingPage(EmbeddingPage):
    """A page of a ranked scan.

    `total` is not a promise that every row is worth reading: the tail of a
    ranked scan is whatever scored least, not a further set of matches. The
    scores are in the response so a client can cut its own cut-off.
    """

    #: How many chunks were scored after filtering. Lets a client tell
    #: "nothing matched" from "the filters left nothing to match". Equal to
    #: `total` except on `/similar`, which never returns its own source.
    candidates: int = 0


class EmbeddingSearchResponse(RankedEmbeddingPage):
    """One page of results for one query.

    Paged with `limit`/`offset` like the rest of the dashboard's lists.
    """

    query: str
    kind: EmbeddingKind
    task: str


class EmbeddingSimilarResponse(RankedEmbeddingPage):
    """One page of the neighbours of a chunk already in the corpus."""

    source: EmbeddingSearchHit


class EmbeddingCodeSimilarResponse(RankedEmbeddingPage):
    """One page of the corpus ranked against what a code has been applied to.

    "More like this" with a code for the this. Where the single-chunk version
    ranks against one stored vector, this ranks against the average of every
    chunk the code sits on -- so what it is near is the code as it has been
    *used*, which is a different claim from the code as it was *defined*, and
    the two actions in the panel are deliberately both there.
    """

    code_id: UUID4
    #: Chunks the centroid was averaged from. The number that says how much to
    #: trust the ranking: one seed is "more like this chunk" wearing a code's
    #: name, and a code nothing has been applied to cannot be asked at all.
    #:
    #: Those chunks are in the results too, and near the top by construction.
    #: Left in deliberately -- whether the seeds sit together is what the
    #: ranking says about the code itself -- and `-code:x` in the keyword query
    #: is how a reader asks for only where it has not reached.
    seeds: int = 0


class EmbeddingBrowseResponse(EmbeddingPage):
    """One page of the corpus with no query behind it.

    The list view's resting state: the filters alone decide what is in it, and
    guide order decides the sequence. `total` here *is* a count of things worth
    reading, unlike the ranked endpoints -- nothing was scored, so nothing is
    tailing off.
    """

    kind: EmbeddingKind


class EmbeddingStatus(Schema):
    """Whether a project's corpus is embedded, and whether it could be."""

    enabled: bool
    # Whether interviews embed as they run. When false, a project's coverage
    # only moves when a backfill is requested.
    live: bool = False
    healthy: bool
    model: str
    dimension: int
    # Stored vectors per `EmbeddingKind`, for this project.
    coverage: dict[str, int] = {}
    # Stored vectors per language code, most-embedded first. Reported here so a
    # client can offer a language filter whose options are the project's, not
    # whatever the last query happened to return.
    languages: dict[LanguageCode, int] = {}
    total: int = 0
    # Live queue, process-wide rather than per project: chunks waiting to be
    # embedded, and chunks dropped because the queue was full since startup.
    # A non-zero drop count is not data loss -- the backfill re-derives them --
    # but it does mean search results are behind.
    queue_depth: int = 0
    queue_dropped: int = 0


class SurveyFacetValue(Schema):
    """One answer on offer in the survey filter, with how many gave it."""

    #: Position in the item's option list, or None for a write-in. What the
    #: filter is expressed in: the same option is the same position in every
    #: language the item was asked in, and its text is not.
    option: int | None = None
    #: What to show for it. The authored wording where the guide still has the
    #: item, the respondent's own where it does not.
    label: str
    #: Interviews whose respondent gave this answer. Interviews rather than
    #: answers because that is what the filter selects -- picking a value with
    #: `count` beside it should not be able to return chunks from more
    #: interviews than it named.
    count: int


class SurveyFacet(Schema):
    """One survey item, as something to filter a cohort by."""

    section: int
    main_question: int
    #: The question as authored in the project's default localization, falling
    #: back to the wording an interview actually asked.
    question: str
    #: The survey item's own type -- `radio`, `slider`, `date` and so on.
    type: str
    #: How it is filtered: `values` for a checklist of options, `range` for the
    #: ordered ones. Derived from the type here rather than in the client, so
    #: the two cannot disagree about what a control to draw for a `likert` is.
    filter: Literal["values", "range"]
    #: Whether one respondent can hold several of these values at once, which
    #: is a checkbox. It changes what selecting two of them means.
    multiple: bool = False
    #: The answers actually given, commonest first among the write-ins.
    #: Authored options are always listed, in their authored order, even at
    #: zero -- "nobody chose this" is worth being able to see before filtering
    #: by it.
    values: list[SurveyFacetValue] = []
    #: The lowest and highest answer, as text in the item's own spelling: a
    #: number, or an ISO date, datetime or time. What the range control opens
    #: on, so a slider does not have to guess its own ends.
    low: str | None = None
    high: str | None = None
    #: Interviews that answered this item at all. The denominator the counts
    #: above are read against.
    n_answered: int = 0


class SurveyFacets(Schema):
    """Every survey item a project's interviews carry an answer to."""

    items: list[SurveyFacet] = []


class CodeFacet(Schema):
    """One code, with how much of the current view carries it."""

    code_id: UUID4
    #: Chunks coded with this code exactly. What clicking *filter* on the row
    #: would return, so a badge is never an invitation into an empty view.
    count: int = 0
    #: Chunks coded with it or with anything under it -- what `/*` returns.
    #: Counted over the union of the branch and not summed down it: a chunk
    #: carrying both a parent and its child is one chunk, and adding the rows
    #: up would report it twice.
    subtree: int = 0


class CodeFacets(Schema):
    """What each code in the codebook is worth over the chunks now in view."""

    #: The unit the counts are in. A code applied to one message is one
    #: MESSAGE, one QA_PAIR and one INTERVIEW, so the number only means
    #: something next to the unit it was counted in.
    kind: EmbeddingKind
    #: Chunks in view at all -- the denominator the counts are read against.
    total: int = 0
    #: Only the codes something in view carries; a code at zero is left out
    #: and the client reads a missing code as zero.
    items: list[CodeFacet] = []


class EmbeddingBackfillResponse(Schema):
    """What one backfill trigger put in flight."""

    queued: int
    # Chunks that needed embedding but did not fit in the queue. They are not
    # lost -- the next trigger or a CLI run picks them up -- but they are not
    # coming in this round either.
    skipped: int
    # Interviews whose stored history could not be reconstructed, usually a
    # guide snapshot that no longer matches the messages recorded against it.
    failed_interviews: list[str] = []
    queue_depth: int


class EmbeddingClusterPoint(Schema):
    """One chunk's position in the scatter plot."""

    id: UUID4
    # None means HDBSCAN declined to place it. Outliers are kept rather than
    # dropped: in interview data the unplaceable answers are often the ones
    # worth reading.
    cluster: int | None
    # HDBSCAN's confidence that the point belongs to its cluster, 0 for outliers.
    probability: float
    x: float
    y: float
    preview: str | None = None
    # The interview-guide coordinates the chunk came from, so the same scatter
    # can be coloured by what the guide asked rather than by what clustering
    # found. NULL on an interview chunk, which spans the whole guide.
    section: int | None = None
    main_question: int | None = None
    sub_question: int | None = None
    # The interview's language. Sent for the same reason as the guide
    # coordinates: on a multilingual project the model separates languages
    # before it separates topics, and colouring by this is how that becomes
    # visible instead of being mistaken for two themes.
    language: LanguageCode


class EmbeddingCluster(Schema):
    id: int
    size: int
    # Members nearest the cluster centre, in full: the material for naming it.
    representatives: list[EmbeddingSearchHit] = []
    # Share of members from the single most common interview question. Near 1.0
    # means the cluster is really just that question -- a QA-pair chunk repeats
    # its question verbatim for every respondent, so uncentred clustering tends
    # to recover the interview guide. Read this before reading the clusters.
    question_purity: float | None = None
    # Share of members from the single most common language. Near 1.0 on a
    # project that ran in more than one means the cluster is a language: the
    # model separates Danish from English more strongly than it separates
    # anything either of them says. Always 1.0 when only one language is in
    # scope, where it means nothing -- read it against `groups`.
    language_purity: float | None = None


class EmbeddingGroup(Schema):
    """A named set of points to colour the scatter by.

    Questions and sections come from the guide rather than from the data, so
    unlike a cluster they arrive already named -- which is what makes them the
    baseline worth reading the clusters against.
    """

    kind: GroupKind
    # `"<section>"` for a section, `"<section>.<main_question>"` for a question.
    # A string because it is a compound key and clients use it as a dictionary
    # key, not as a number.
    key: str
    # As the guide numbers it: "Q2.1" or "Section 2".
    label: str
    # The guide's own wording, when the question still exists in the current
    # draft. NULL for a question an interview asked under an older snapshot.
    text: str | None = None
    size: int


class EmbeddingClusterResponse(Schema):
    kind: EmbeddingKind
    n_points: int
    n_clusters: int
    n_outliers: int
    # How the vectors were reduced. Under "pca" the scatter is a linear
    # projection, so distances on it are comparable everywhere; under "umap"
    # only adjacency is meaningful -- who sits next to whom, never how far
    # apart two clusters are or how big one looks.
    projection: Projection
    # Dimensions clustering ran in. The scatter shows the first two of them,
    # and under "umap" there are only those two.
    components: int
    # Share of total variance the two plotted axes carry. Typically low for text
    # embeddings: the plot is a navigation aid, not evidence. Points far apart
    # on screen are genuinely far apart; points close together may not be.
    # NULL under "umap", which has no such quantity.
    explained_variance_2d: float | None = None
    centered_by_question: bool
    # Whether each language's mean vector was subtracted before projection. On a
    # multilingual project this is what stops the biggest clusters from simply
    # being the languages.
    centered_by_language: bool = False
    clusters: list[EmbeddingCluster] = []
    # Every guide group the plotted points fall into, questions and sections
    # alike, ordered as the guide orders them. Sent alongside the clusters
    # rather than behind a parameter: it is derived from the same points, costs
    # one guide read, and lets a client switch grouping without a round trip.
    groups: list[EmbeddingGroup] = []
    points: list[EmbeddingClusterPoint] = []


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


class EmbeddingFilterParams(Schema):
    """The non-vector half of a search, as a reusable dependency.

    Filtering happens in SQL before anything is scored, so these narrow the
    candidate set rather than the result list -- asking for 10 results from one
    participant returns 10 of theirs, not whichever of the global top 10
    happened to be theirs. For clustering the same is true of the reduction:
    filtered-out chunks are never fitted, so scoping to one language removes
    that dimension outright rather than subtracting its mean.

    `language` is repeatable (`?language=DA&language=EN`), because a
    multilingual project is usually analysed over the languages that have
    enough respondents to say anything -- rarely all of them, rarely just one.

    `question` is repeatable in the same way and written `?question=0,2` --
    zero-based `section,main_question`, the spelling the annotate view already
    uses so one filter means the same thing in both places. A whole section is
    asked for by listing its questions.

    It is read against whatever unit is being asked for, because the units do
    not all carry a question. A message or a QA pair matches its coordinates
    exactly; a section matches when the selection names a question inside it; an
    interview matches when the transcript contains an answer to one. Demanding
    the exact pair of every unit would empty the two spanning ones outright,
    which is a filter that cannot be used rather than a filter that says no.

    `keyword` is the literal half of searching, and it is a filter rather than a
    query: it narrows the candidate set, and whatever semantic query there is
    then ranks what survives. Matched against respondent messages, never against
    chunk text -- a chunk restates the question it answers, and a word the
    interviewer said is not a word the respondent said.

    `keyword_scope` says which side of the exchange a bare term is matched
    against -- `answer` (the default and the historical behaviour), `question`,
    or `both` -- and `q:`/`a:` inside the query override it for a single term.
    Matching the question at all is deliberate rather than free: a chunk restates
    the question it answers, so counting the interviewer's words by default would
    let the guide's own phrasing read as a finding.

    `survey` filters by what the interview's respondent answered to a survey
    item, and it is a *cohort* filter: every chunk of a matching interview
    stays, whatever question it answers. Written `?survey=0,2=option:1` --
    guide coordinate, then the value -- and repeatable, with the values of one
    item OR-ed and different items AND-ed.

    A value is named by its position in the option list (`option:1`) rather
    than by its text, because the same item asked in two languages offers the
    same choices translated: filtering by "Female" would silently drop everyone
    interviewed in Danish. A write-in, which has no position, is named by its
    text instead (`text:kayaking`).

    `survey_range` is the same filter for the items whose answers are ordered
    rather than chosen -- numbers, dates, times. Written
    `?survey_range=0,3=25..34`, with either side allowed to be empty for an
    open end, and the bounds spelled the way the answers are: a number, or an
    ISO date, datetime or time.

    `coded_mine` and `coded_others` are coverage rather than content: `any`
    keeps the chunks that coder has marked, `none` the chunks they have not,
    and `coder_id` says who "mine" is. Deliberately not `code:` with a NOT in
    front -- a negated term is checked per message and then lifted, so a
    section holding one coded turn and one uncoded one satisfies it, while this
    asks about the chunk. `none` is the pass that closes a codebook.

    `coder_join` joins the two, and is what makes them a complete 2x2. `and`
    names the quadrants: the second-coder pass -- what have they coded that I
    have not -- is `coded_mine=none&coded_others=any`, and there is no single
    scope it is a filter on. `or` names their complements, of which two are
    questions worth asking: `any` or `any` is "somebody has coded this", the
    one reading that is a disjunction, and `none` or `none` is "not coded by
    both" -- the work left in a double-coding pass.

    An axis left out does not participate under either operator. If "unset"
    meant *true* under `or`, leaving one out would widen the corpus to
    everything rather than leave it alone.

    `main_answers_only` keeps the answers to main questions and leaves out the
    probes. It narrows the message unit into the "main question" one the
    explorer offers, over the same vectors, and is ignored by the grouped units.

    None of them narrows a `code:` term, which counts anybody's codings. A
    coder-scoped code term is a question about the term rather than about
    coverage, and folding it in here would silently empty the review pass --
    under `coded_mine=none` it could not match by construction.

    It is a boolean expression rather than a string to look for: `dog OR cat`,
    `kids -school`, `(dog OR cat) AND "my neighbour"`. `app.db.keyword_query`
    has the grammar. Matching is case-insensitive and by word, with `*` to open
    an edge (`kat*`) and quotes for a phrase. A query that will not parse is a
    422 saying what is wrong and where, rather than a search for something other
    than what was asked for.
    """

    language: list[LanguageFilter] | None = None
    status: InterviewStatus | None = None
    participant_id: UUID4 | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None
    interview_id: list[UUID4] | None = None
    include_synthetic: bool = False
    question: list[str] | None = None
    keyword: str | None = Field(default=None, max_length=2000)
    keyword_scope: Scope = "answer"
    survey: list[str] | None = None
    survey_range: list[str] | None = None
    coded_mine: Coded | None = None
    coded_others: Coded | None = None
    coder_join: CoderJoin = "and"
    coder_id: UUID4 | None = None
    main_answers_only: bool = False


# How far into a ranking `offset` may reach. A ranked scan has no natural end
# -- every chunk in scope gets a score -- so without a bound a client can page
# a whole project out through an endpoint meant for retrieval. A thousand rows
# in is already well past where scores stop meaning anything; a corpus is read
# with the export or the cluster map, not with the search endpoint.
MAX_SEARCH_DEPTH = 1000


class SearchPageParams(Schema):
    """`limit`/`offset` for the two ranked endpoints.

    Named as the dashboard's other lists name them, but deliberately not
    `PageParams`: that carries `column` and `order`, and a ranked
    scan has exactly one order -- by score -- which a caller cannot choose.
    """

    limit: int = Field(default=10, ge=1, le=100)
    offset: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _within_search_depth(self) -> SearchPageParams:
        if self.offset + self.limit > MAX_SEARCH_DEPTH:
            raise ValueError(
                f"offset + limit may not exceed {MAX_SEARCH_DEPTH}; results "
                "that far down a ranked list are noise, not further matches"
            )
        return self
