import re
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import UUID4
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from starlette.concurrency import run_in_threadpool

from ainterviewer.constants import LANGUAGES
from ainterviewer.interview_guides import SurveyItem
from ainterviewer.interview_guides.survey_items import CheckboxItem
from ainterviewer.types import EmbeddingKind, InterviewStatus

from ....db.keyword_query import MARKUP_PATTERN, KeywordQueryError, Scope, parse
from ....db.models import (
    EmbeddingBackfillResponse,
    EmbeddingBrowseResponse,
    EmbeddingCluster,
    EmbeddingClusterPoint,
    EmbeddingClusterResponse,
    EmbeddingGroup,
    EmbeddingSearchHit,
    EmbeddingSearchResponse,
    EmbeddingSimilarResponse,
    EmbeddingStatus,
    InterviewTranscript,
    SurveyFacet,
    SurveyFacets,
    SurveyFacetValue,
)
from ....db.repositories.embedding import ChunkCoordinates, EmbeddingFilters
from ....db.survey_answers import (
    CATEGORICAL_TYPES,
    NUMERIC_TYPES,
    TEMPORAL_TYPES,
    Coordinate,
    OptionValue,
    Range,
    SurveyFilter,
    TextValue,
    Value,
    answer_rows,
    normalize_answer,
    options_of,
    values_of,
)
from ....db.tables import ProjectLocalizationTable
from ....dependencies import DBSession, ProjectEditor, ProjectViewer
from ....embed.backfill import pending_chunks
from ....embed.client import EmbeddingUnavailable, embedding_client
from ....embed.clustering import (
    DEFAULT_MIN_CLUSTER_SIZE,
    DEFAULT_MIN_DIST,
    DEFAULT_N_NEIGHBORS,
    GroupAxis,
    cluster_vectors,
)
from ....embed.queue import chunk_queue
from ....embed.templates import QueryTask
from ....settings import app_settings
from ....types import GroupKind, Projection
from ...request_models import LanguageFilter

router = APIRouter()


class SearchFilterParams:
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

    It is a boolean expression rather than a string to look for: `dog OR cat`,
    `kids -school`, `(dog OR cat) AND "my neighbour"`. `app.db.keyword_query`
    has the grammar. Matching is case-insensitive and by word, with `*` to open
    an edge (`kat*`) and quotes for a phrase. A query that will not parse is a
    422 saying what is wrong and where, rather than a search for something other
    than what was asked for.
    """

    def __init__(
        self,
        language: Annotated[list[LanguageFilter] | None, Query()] = None,
        status: InterviewStatus | None = None,
        participant_id: UUID4 | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        interview_id: Annotated[list[UUID4] | None, Query()] = None,
        include_synthetic: bool = False,
        question: Annotated[list[str] | None, Query()] = None,
        keyword: Annotated[str | None, Query(max_length=2000)] = None,
        keyword_scope: Scope = "answer",
        survey: Annotated[list[str] | None, Query()] = None,
        survey_range: Annotated[list[str] | None, Query()] = None,
    ):
        self.filters = EmbeddingFilters(
            interview_ids=interview_id,
            languages=language,
            status=status,
            participant_id=participant_id,
            created_after=created_after,
            created_before=created_before,
            include_synthetic=include_synthetic,
            questions=_parse_questions(question),
            keyword=_checked_keyword(keyword),
            keyword_scope=keyword_scope,
            survey=_parse_survey(survey, survey_range),
        )


def _parse_questions(raw: list[str] | None) -> list[tuple[int, int]] | None:
    """`["0,2", "1,0"]` to `[(0, 2), (1, 0)]`, or 422 saying which one was bad.

    Guide coordinates are a pair and FastAPI has no query type for one, so they
    arrive as text and are checked here. Rejecting the malformed value by name
    beats a filter that silently matches nothing -- the difference between a
    typo and an empty corpus is not something a reader can see on the map.
    """
    if not raw:
        return None

    questions: list[tuple[int, int]] = []
    for value in raw:
        parts = value.split(",")
        if len(parts) != 2:
            raise HTTPException(
                422,
                detail=(
                    f"question {value!r} is not a 'section,main_question' pair, "
                    "e.g. '0,2'"
                ),
            )
        try:
            section, main_question = (int(part) for part in parts)
        except ValueError:
            raise HTTPException(
                422,
                detail=(
                    f"question {value!r} is not a 'section,main_question' pair "
                    "of whole numbers, e.g. '0,2'"
                ),
            ) from None
        if section < 0 or main_question < 0:
            raise HTTPException(
                422,
                detail=(
                    f"question {value!r} has a negative index; guide "
                    "coordinates are zero-based and count up"
                ),
            )
        pair = (section, main_question)
        # Deduplicated because the same question arriving twice would widen
        # nothing and only lengthen the OR the scan is filtered by.
        if pair not in questions:
            questions.append(pair)

    return questions


def _survey_coordinate(param: str, value: str) -> tuple[Coordinate, str]:
    """Split `"0,2=option:1"` into `((0, 2), "option:1")`, or 422 saying why not.

    The coordinate is separated from the value by the first `=`, which no guide
    coordinate can contain -- so a value holding one, as a write-in easily
    might, still arrives whole.
    """
    coordinate, separator, rest = value.partition("=")
    if not separator:
        raise HTTPException(
            422,
            detail=(
                f"{param} {value!r} is not a 'section,main_question=value' "
                "selection, e.g. '0,2=option:1'"
            ),
        )
    pairs = _parse_questions([coordinate])
    if not pairs:
        raise HTTPException(422, detail=f"{param} {value!r} names no question")
    return pairs[0], rest


def _parse_survey_values(raw: list[str] | None) -> dict[Coordinate, tuple[Value, ...]]:
    """`["0,2=option:1", "0,2=text:kayaking"]` to the values chosen per item."""
    chosen: dict[Coordinate, list[Value]] = {}
    for entry in raw or []:
        coordinate, rest = _survey_coordinate("survey", entry)
        kind, separator, body = rest.partition(":")
        if not separator or kind not in {"option", "text"}:
            raise HTTPException(
                422,
                detail=(
                    f"survey value {rest!r} is neither 'option:<n>' nor 'text:<answer>'"
                ),
            )

        if kind == "option":
            try:
                position = int(body)
            except ValueError:
                raise HTTPException(
                    422, detail=f"survey value {rest!r} has a non-numeric option"
                ) from None
            if position < 0:
                raise HTTPException(
                    422,
                    detail=(
                        f"survey value {rest!r} has a negative option; option "
                        "positions are zero-based and count up"
                    ),
                )
            value: Value = OptionValue(position)
        else:
            # Normalized here so the filter compares the way the answers were
            # counted -- an option and a write-in only ever differ by whether
            # the item had it, never by spacing or case.
            value = TextValue(normalize_answer(body))

        values = chosen.setdefault(coordinate, [])
        # Deduplicated: the same value twice widens nothing.
        if value not in values:
            values.append(value)

    return {coordinate: tuple(values) for coordinate, values in chosen.items()}


def _parse_survey_ranges(raw: list[str] | None) -> dict[Coordinate, Range]:
    """`["0,3=25..34"]` to a range per item, either end allowed to be open."""
    ranges: dict[Coordinate, Range] = {}
    for entry in raw or []:
        coordinate, rest = _survey_coordinate("survey_range", entry)
        low, separator, high = rest.partition("..")
        if not separator:
            raise HTTPException(
                422,
                detail=(
                    f"survey_range {rest!r} is not a 'low..high' range; leave a "
                    "side empty for an open end, e.g. '25..' or '..34'"
                ),
            )
        if not low and not high:
            # Both ends open is every answer, which is what asking for no
            # filter looks like -- and a range nothing can fail is not one.
            continue
        ranges[coordinate] = Range(low=low or None, high=high or None)
    return ranges


def _parse_survey(
    values: list[str] | None, ranges: list[str] | None
) -> SurveyFilter | None:
    """The survey filter both parameters describe, or None where neither asks
    for anything."""
    parsed = SurveyFilter(
        values=_parse_survey_values(values), ranges=_parse_survey_ranges(ranges)
    )
    return parsed or None


def _checked_keyword(raw: str | None) -> str | None:
    """The keyword query, parsed here so a bad one is a 422 and not a 500.

    Parsed and thrown away rather than passed on as a tree: the repository takes
    a string and parses it itself, so that a caller which never touches this
    endpoint -- a script, a test -- gets the same language. Parsing twice costs
    nothing next to the scan, and it buys an error raised where FastAPI can turn
    it into a response.

    The detail is an object rather than a sentence because the client points at
    the offending character with it.
    """
    if raw is None or not raw.strip():
        return raw

    try:
        parse(raw)
    except KeywordQueryError as error:
        raise HTTPException(
            422,
            detail={
                "error": "invalid_keyword_query",
                "message": error.message,
                "position": error.position,
            },
        ) from None
    return raw


# How far into a ranking `offset` may reach. A ranked scan has no natural end
# -- every chunk in scope gets a score -- so without a bound a client can page
# a whole project out through an endpoint meant for retrieval. A thousand rows
# in is already well past where scores stop meaning anything; a corpus is read
# with the export or the cluster map, not with the search endpoint.
MAX_SEARCH_DEPTH = 1000


class SearchPageParams:
    """`limit`/`offset` for the two ranked endpoints.

    Named as the dashboard's other lists name them, but deliberately not
    `PaginatedQueryParams`: that carries `column` and `order`, and a ranked
    scan has exactly one order -- by score -- which a caller cannot choose.
    """

    def __init__(
        self,
        limit: Annotated[int, Query(ge=1, le=100)] = 10,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        if offset + limit > MAX_SEARCH_DEPTH:
            raise HTTPException(
                422,
                detail=(
                    f"offset + limit may not exceed {MAX_SEARCH_DEPTH}; results "
                    "that far down a ranked list are noise, not further matches"
                ),
            )
        self.limit = limit
        self.offset = offset


@router.get("/projects/{project_id}/analysis/embeddings/search")
async def search_embeddings(
    project_id: UUID4,
    db: DBSession,
    jwt: ProjectViewer,
    filter_params: Annotated[SearchFilterParams, Depends()],
    page: Annotated[SearchPageParams, Depends()],
    query: Annotated[str, Query(min_length=1, max_length=2000)],
    kind: EmbeddingKind = EmbeddingKind.QA_PAIR,
    task: QueryTask = QueryTask.RETRIEVAL,
) -> EmbeddingSearchResponse:
    """Semantic search over one project's embedded interview text.

    `task` selects the instruction the *query* is templated under; the stored
    vectors are task-free, so switching tasks costs a query embedding and
    nothing else. `kind` picks the unit searched -- QA pairs by default, since a
    lone answer is often too short to mean anything out of context.

    Paged with `limit`/`offset`, which together may not reach further than
    `MAX_SEARCH_DEPTH` into the ranking. Every page re-embeds the query and
    re-scores the candidate set, which is what keeps a page a function of the
    query and the corpus rather than of a cached ranking -- and `total` is the
    length of that ranking, not a count of things worth reading. Deep pages of
    a semantic search are the chunks that scored least.
    """
    if not embedding_client.enabled:
        raise HTTPException(503, detail="Embedding is not enabled on this deployment")

    try:
        query_vector = await embedding_client.embed_query(query, task)
    except EmbeddingUnavailable as error:
        raise HTTPException(503, detail=f"Embedding server unavailable: {error}")

    try:
        result = db.embeddings.search(
            project_id=project_id,
            query_vector=query_vector,
            kind=kind,
            limit=page.limit,
            offset=page.offset,
            filters=filter_params.filters,
        )
    except ValueError as error:
        # Raised when the query's dimension does not match what is stored,
        # i.e. the model changed and the corpus has not been re-embedded.
        raise HTTPException(409, detail=str(error))

    turns = db.embeddings.turns_for(
        [hit.embedding for hit in result.hits], filter_params.filters
    )

    return EmbeddingSearchResponse(
        query=query,
        kind=kind,
        task=task,
        candidates=result.scored,
        total=result.total,
        offset=page.offset,
        items=[
            EmbeddingSearchHit.from_hit(
                hit.embedding, hit.score, turns.get(hit.embedding.id)
            )
            for hit in result.hits
        ],
    )


@router.get("/projects/{project_id}/analysis/embeddings/browse")
async def browse_embeddings(
    project_id: UUID4,
    db: DBSession,
    jwt: ProjectViewer,
    filter_params: Annotated[SearchFilterParams, Depends()],
    page: Annotated[SearchPageParams, Depends()],
    kind: EmbeddingKind = EmbeddingKind.QA_PAIR,
) -> EmbeddingBrowseResponse:
    """The corpus in guide order, with no query and no vectors.

    The resting state of the list view, and the one endpoint here that does not
    need the corpus to have been embedded: it reads message rows and groups them
    into the requested unit itself. That is deliberate. Keyword search and
    structural filtering are things a researcher should be able to do on the day
    they finish collecting, not after somebody remembers to run a backfill.

    Where the project *has* been embedded, each unit is matched back to its
    stored vector, so a row browsed here can still be asked what it is near.
    `embedded` on each item says whether that is available.

    Paged with `limit`/`offset` like the ranked endpoints, but `total` means
    something stronger here: nothing was scored, so every row it counts is a row
    that matched the filters rather than the tail of a ranking.
    """
    result = db.embeddings.browse(
        project_id=project_id,
        kind=kind,
        filters=filter_params.filters,
        limit=page.limit,
        offset=page.offset,
    )

    turns = db.embeddings.turns_for(result.units, filter_params.filters)

    return EmbeddingBrowseResponse(
        kind=kind,
        total=result.total,
        offset=page.offset,
        items=[
            EmbeddingSearchHit.from_hit(unit, None, turns.get(unit.id))
            for unit in result.units
        ],
    )


# The longest tail of write-in answers the picker is offered. A conversational
# interview can put a distinct wording on nearly every respondent, and a filter
# listing four hundred one-interview values is a filter nobody can read.
MAX_WRITE_INS = 20

_MARKUP = re.compile(MARKUP_PATTERN)


def _facet_filter(item) -> tuple[Literal["values", "range"], bool] | None:
    """How an item is filtered, or None where it cannot be.

    A free-text question has no survey item and so nothing to offer: its
    answers are what the keyword filter is for.
    """
    if isinstance(item, CATEGORICAL_TYPES):
        return "values", isinstance(item, CheckboxItem)
    if isinstance(item, NUMERIC_TYPES + TEMPORAL_TYPES):
        return "range", False
    return None


def _facet_values(rows: list, options: list[str]) -> tuple[list[SurveyFacetValue], int]:
    """Every answer on offer for one categorical item, and how many gave it.

    Counted by interview and not by answer: the two only differ for a checkbox,
    where one respondent holds several values, and a count that read higher
    than the number of interviews it can return would be a count of the wrong
    thing.

    The authored options are always listed, in order and even at zero. The
    write-ins follow, commonest first, because they have no order of their own
    and no promise that there are few of them.
    """
    by_option: dict[int, set[UUID]] = {}
    write_ins: dict[str, tuple[str, set[UUID]]] = {}

    for row in rows:
        for value in values_of(row):
            if isinstance(value, OptionValue):
                by_option.setdefault(value.index, set()).add(row.interview_id)
            else:
                # The first spelling seen is the one shown; the key is the
                # normalized form, which is what the filter matches on.
                _, seen = write_ins.setdefault(value.text, (value.text, set()))
                seen.add(row.interview_id)

    values = [
        SurveyFacetValue(
            option=position,
            label=option,
            count=len(by_option.get(position, ())),
        )
        for position, option in enumerate(options)
    ]

    # Positions past the authored list: the item offered more options when
    # these interviews ran than the guide does now. Kept rather than dropped --
    # somebody answered them -- and labelled by position, which is all that is
    # left of them.
    for position in sorted(set(by_option) - set(range(len(options)))):
        values.append(
            SurveyFacetValue(
                option=position,
                label=f"Option {position + 1}",
                count=len(by_option[position]),
            )
        )

    ranked = sorted(write_ins.values(), key=lambda entry: (-len(entry[1]), entry[0]))
    values.extend(
        SurveyFacetValue(option=None, label=label, count=len(seen))
        for label, seen in ranked[:MAX_WRITE_INS]
    )

    answered = {row.interview_id for row in rows}
    return values, len(answered)


def survey_facets(session, project_id: UUID, include_synthetic: bool) -> SurveyFacets:
    """The survey answers a project's interviews hold, as things to filter by.

    A plain function rather than the endpoint itself so it can be read against
    a database in a test, and so the session work happens in one place the
    route can hand to a threadpool.

    It reports the answers respondents *gave* rather than the options the guide
    offers, for two reasons: a write-in has no authored option and would
    otherwise be unfilterable, and a value with no interviews behind it is a
    filter that empties the view with nothing on screen to explain why.
    Authored options are still listed at zero, which says the same thing before
    it happens.

    Answers to a question the guide no longer has are still answers, and are
    carried on the wording and options the interviews were actually asked with.
    """
    rows = answer_rows(session, project_id, include_synthetic=include_synthetic)

    by_coordinate: dict[Coordinate, list] = {}
    for row in rows:
        by_coordinate.setdefault(row.coordinate, []).append(row)

    # The current draft, for the authored wording and the option labels. The
    # interviews ran against per-interview snapshots, so this is what the
    # questions are *called* and not what they were.
    guide = session.execute(
        select(ProjectLocalizationTable.interview_guide).where(
            ProjectLocalizationTable.project_id == project_id,
            ProjectLocalizationTable.is_default.is_(True),
        )
    ).scalar_one_or_none()

    authored: dict[Coordinate, tuple[str, SurveyItem | None]] = {}
    if guide is not None:
        for section_index, section in enumerate(guide.question_sections):
            for question_index, question in enumerate(section.questions):
                authored[(section_index, question_index)] = (
                    question.main_question,
                    question.survey_item,
                )

    items: list[SurveyFacet] = []
    for coordinate in sorted(by_coordinate):
        here = by_coordinate[coordinate]
        observed = here[0].item
        shape = _facet_filter(observed)
        if shape is None:
            continue
        filter_kind, multiple = shape

        question, draft_item = authored.get(coordinate, ("", None))
        # Guide questions carry markup -- an underlined word, a link -- and the
        # picker draws this as one truncated line of text, where a literal
        # `<u>` is noise. The keyword filter strips the same tags out of the
        # question side for the same reason: they are not words anybody said.
        question = _MARKUP.sub("", question).strip()

        facet = SurveyFacet(
            section=coordinate[0],
            main_question=coordinate[1],
            question=question,
            type=str(observed.type),
            filter=filter_kind,
            multiple=multiple,
        )

        if filter_kind == "values":
            observed_options = options_of(observed) or []
            draft_options = options_of(draft_item)
            # The draft's wording is preferred, but only while it describes the
            # same list: an item edited to offer a different number of options
            # is no longer describing the answers these interviews gave, and
            # labelling position 2 with whatever now sits there would put a
            # word in a respondent's mouth.
            options = (
                draft_options
                if draft_options is not None
                and len(draft_options) == len(observed_options)
                else observed_options
            )
            facet.values, facet.n_answered = _facet_values(here, options)
        else:
            ordered = sorted(row.content.strip() for row in here)
            facet.low, facet.high = (
                (ordered[0], ordered[-1]) if ordered else (None, None)
            )
            facet.n_answered = len({row.interview_id for row in here})

        items.append(facet)

    return SurveyFacets(items=items)


@router.get("/projects/{project_id}/analysis/embeddings/survey-facets")
async def read_survey_facets(
    project_id: UUID4,
    db: DBSession,
    jwt: ProjectViewer,
    include_synthetic: bool = False,
) -> SurveyFacets:
    """What the cohort filter in the explore view is built from.

    Deliberately not narrowed by the filters the view currently has applied.
    The counts would then move as the reader filters, and an option that
    reached zero would vanish from under the selection that produced it --
    leaving no way back. The language filter is offered from the whole corpus
    for the same reason.
    """
    return await run_in_threadpool(
        survey_facets, db.session, project_id, include_synthetic
    )


@router.get(
    "/projects/{project_id}/analysis/embeddings/interviews/{interview_id}/transcript"
)
async def read_interview_transcript(
    project_id: UUID4,
    interview_id: UUID4,
    db: DBSession,
    jwt: ProjectViewer,
    keyword: Annotated[str | None, Query()] = None,
    keyword_scope: Scope = "answer",
) -> InterviewTranscript:
    """One interview in full, with the keyword query marked in it.

    The context around a hit. A card shows a question group; the question of
    whether an answer means what it appears to mean is usually settled by what
    was said just before or just after it, and that is a different unit than
    anything the mosaic can show.

    Deliberately not the messages endpoint the transcript page loads. That one
    carries annotations, comments and audio, and knows nothing about a keyword
    query; this one carries the query's marks and nothing else, because the
    reader arriving here arrived from a search and the first thing they need is
    to see where it hit. Annotating remains the page's, which this links to.

    `keyword` is optional and validated the same way the search endpoints
    validate it, so a query that cannot be read is a 422 here too rather than a
    transcript quietly rendered with nothing marked.
    """
    try:
        turns = db.embeddings.transcript(
            project_id=project_id,
            interview_id=interview_id,
            keyword=_checked_keyword(keyword),
            keyword_scope=keyword_scope,
        )
    except NoResultFound:
        raise HTTPException(404, detail="Interview not found")

    return InterviewTranscript(interview_id=interview_id, turns=turns)


@router.get("/projects/{project_id}/analysis/embeddings/{embedding_id}/similar")
async def find_similar_embeddings(
    project_id: UUID4,
    embedding_id: UUID4,
    db: DBSession,
    jwt: ProjectViewer,
    filter_params: Annotated[SearchFilterParams, Depends()],
    page: Annotated[SearchPageParams, Depends()],
) -> EmbeddingSimilarResponse:
    """Chunks most like an existing one -- "more like this".

    Costs no inference: the query vector is the one already stored, so this
    works even when the embedding server is down. Searches within the source's
    own kind and never returns the source itself, which is why `total` is one
    below `candidates` whenever the source survives the filters.

    Paged with `limit`/`offset` on the same terms as the search endpoint.
    """
    try:
        source, result = db.embeddings.similar_to(
            embedding_id=embedding_id,
            limit=page.limit,
            offset=page.offset,
            filters=filter_params.filters,
        )
    except NoResultFound:
        raise HTTPException(404, detail="Embedding not found")
    except ValueError as error:
        raise HTTPException(409, detail=str(error))

    if source.project_id != project_id:
        # The route is authorised on project_id, so a chunk from elsewhere must
        # not be reachable through it.
        raise HTTPException(404, detail="Embedding not found")

    turns = db.embeddings.turns_for(
        [source, *(hit.embedding for hit in result.hits)], filter_params.filters
    )

    return EmbeddingSimilarResponse(
        source=EmbeddingSearchHit.from_hit(source, 1.0, turns.get(source.id)),
        candidates=result.scored,
        total=result.total,
        offset=page.offset,
        items=[
            EmbeddingSearchHit.from_hit(
                hit.embedding, hit.score, turns.get(hit.embedding.id)
            )
            for hit in result.hits
        ],
    )


PREVIEW_CHARS = 240

# Guide question text is a label on a legend row, not a card: enough to
# recognise the question, not to re-read it.
GROUP_TEXT_CHARS = 120


LANGUAGE_NAMES = {entry["code"]: entry["name"] for entry in LANGUAGES}


def _point_groups(
    session,
    project_id: UUID4,
    coordinates: list[ChunkCoordinates],
) -> list[EmbeddingGroup]:
    """The declared groups the plotted points fall into: sections, questions,
    languages.

    These are the baseline the clusters are read against. If colouring by one of
    them reproduces the clustering, the clustering found scaffolding -- the
    guide, or the respondent's language -- and not a theme. The language rows
    exist because that failure is otherwise invisible: on a Danish/English
    project the two biggest clusters were simply the two languages, and nothing
    in the response said so.

    Sized from the points rather than from the guide: a question nobody
    answered is not a colour on this map, and an empty legend row would be one.
    The wording comes from the project's default localization, which is the
    current draft -- interviews ran against per-interview snapshots, so a
    question the draft has since dropped keeps its number and loses only its
    text.

    The indices on a chunk are positions in the order the respondent was
    actually asked, which for a shuffled section is not the guide's authored
    order; pairing them with the draft is therefore "the Nth question of
    section M" rather than a specific authored question. The same
    approximation the report and monitoring pages make.
    """
    question_sizes: dict[tuple[int, int], int] = {}
    section_sizes: dict[int, int] = {}
    language_sizes: dict[str, int] = {}
    for point in coordinates:
        language_sizes[point.language] = language_sizes.get(point.language, 0) + 1
        if point.section is None:
            continue
        section_sizes[point.section] = section_sizes.get(point.section, 0) + 1
        if point.main_question is not None:
            key = (point.section, point.main_question)
            question_sizes[key] = question_sizes.get(key, 0) + 1

    # Language needs no guide, so it is built before the early return: an
    # INTERVIEW chunk carries no guide coordinates at all, and its map should
    # still be colourable by language.
    language_groups = [
        EmbeddingGroup(
            kind=GroupKind.LANGUAGE,
            key=code,
            label=code,
            text=LANGUAGE_NAMES.get(code),
            size=size,
        )
        for code, size in sorted(language_sizes.items(), key=lambda kv: -kv[1])
    ]

    if not section_sizes:
        return language_groups

    guide = session.execute(
        select(ProjectLocalizationTable.interview_guide).where(
            ProjectLocalizationTable.project_id == project_id,
            ProjectLocalizationTable.is_default.is_(True),
        )
    ).scalar_one_or_none()

    section_text: dict[int, str] = {}
    question_text: dict[tuple[int, int], str] = {}
    if guide is not None:
        for section_idx, section in enumerate(guide.question_sections):
            if section.description:
                section_text[section_idx] = section.description
            for question_idx, question in enumerate(section.questions):
                question_text[(section_idx, question_idx)] = question.main_question

    def shorten(text: str | None) -> str | None:
        if not text:
            return None
        text = " ".join(text.split())
        if len(text) <= GROUP_TEXT_CHARS:
            return text
        return text[: GROUP_TEXT_CHARS - 1].rstrip() + "\u2026"

    groups = [
        EmbeddingGroup(
            kind=GroupKind.SECTION,
            key=str(section),
            label=f"Section {section + 1}",
            text=shorten(section_text.get(section)),
            size=size,
        )
        for section, size in sorted(section_sizes.items())
    ]
    groups.extend(
        EmbeddingGroup(
            kind=GroupKind.QUESTION,
            key=f"{section}.{main_question}",
            label=f"Q{section + 1}.{main_question + 1}",
            text=shorten(question_text.get((section, main_question))),
            size=size,
        )
        for (section, main_question), size in sorted(question_sizes.items())
    )
    groups.extend(language_groups)
    return groups


@router.get("/projects/{project_id}/analysis/embeddings/clusters")
async def cluster_embeddings(
    project_id: UUID4,
    db: DBSession,
    jwt: ProjectViewer,
    filter_params: Annotated[SearchFilterParams, Depends()],
    kind: EmbeddingKind = EmbeddingKind.QA_PAIR,
    projection: Projection = Projection.UMAP,
    min_cluster_size: Annotated[int, Query(ge=2, le=500)] = DEFAULT_MIN_CLUSTER_SIZE,
    min_samples: Annotated[int | None, Query(ge=1, le=500)] = None,
    n_neighbors: Annotated[int, Query(ge=2, le=200)] = DEFAULT_N_NEIGHBORS,
    min_dist: Annotated[float, Query(ge=0.0, le=1.0)] = DEFAULT_MIN_DIST,
    center_by_question: bool = False,
    center_by_language: bool = False,
    n_representatives: Annotated[int, Query(ge=1, le=10)] = 3,
) -> EmbeddingClusterResponse:
    """Cluster a project's chunks and project them to 2D.

    HDBSCAN in whatever space `projection` reduces to, with the scatter taken
    from the first two dimensions of that *same* space -- so the picture is
    always a sub-projection of where the clusters were found, never a separate
    fit. HDBSCAN rather than k-means because exploratory work does not know `k`
    up front, and because points it cannot place come back as outliers instead
    of being forced into the nearest blob.

    `projection=umap` (the default) reduces non-linearly to 2 dimensions and
    clusters in them. It separates neighbourhoods far more sharply than PCA,
    which is what makes the picture readable, but it costs seconds rather than
    milliseconds, reports no `explained_variance_2d`, and can manufacture a
    split between neighbourhoods that are not really apart -- check a suspicious
    cluster's representatives before believing it. **Distances on a UMAP
    scatter carry no meaning**: read which points sit together, never how far
    apart two clusters are or how large one looks. `projection=pca` is the fast
    linear alternative, and the one to use when the plot's geometry has to mean
    something.

    `n_neighbors` and `min_dist` are read under UMAP only -- the first trades
    local detail against global structure, the second how tightly points may
    pack.

    **Read the purities before reading the clusters.** Two things an embedding
    encodes that are scaffolding rather than content, both of which clustering
    will happily recover instead of a theme:

    - A QA-pair chunk repeats its interview question verbatim, and every
      respondent was asked the same one. `question_purity` near 1.0 means the
      cluster is a question; `center_by_question=true` subtracts each question's
      mean vector first.
    - A multilingual project embeds every language into one space, and the model
      separates languages before it separates topics -- on a Danish/English
      project the two largest clusters were simply Danish and English.
      `language_purity` near 1.0 means the cluster is a language;
      `center_by_language=true` subtracts each language's mean vector.

    Both centre on the composite key when set together, subtracting the mean of
    each language-within-question cell, which removes both confounds in one pass
    and costs no re-embedding. The cost is thinner cells: a cell of one chunk
    becomes the zero vector and collects at the origin. Filtering to a single
    `language` is the blunter alternative -- it analyses one language properly
    instead of comparing across them.

    `groups` carries a `language` row per language in scope alongside the guide
    rows, so the same scatter can be coloured by language directly. That is
    usually the fastest way to see whether a split is real.

    Computed per request rather than stored, so `min_cluster_size` stays an
    interactive control rather than a migration.
    """
    ids, matrix, coordinates = db.embeddings.vectors_for(
        project_id=project_id, kind=kind, filters=filter_params.filters
    )

    axes = [
        # The question a chunk belongs to, not the probe within it: a MESSAGE
        # chunk carries a sub_question, and centring per probe would subtract a
        # different mean from every turn of the same question.
        GroupAxis(
            name=GroupKind.QUESTION,
            keys=[point.question for point in coordinates],
            center=center_by_question,
        ),
        GroupAxis(
            name=GroupKind.LANGUAGE,
            keys=[point.language for point in coordinates],
            center=center_by_language,
        ),
    ]

    # Off the event loop: a UMAP fit is seconds of CPU (and a one-off numba
    # compile on a process's first call), which would otherwise stall every
    # other request in flight. PCA does not need this, but a branch that
    # sometimes blocks the loop is worse than one thread hop.
    result = await run_in_threadpool(
        cluster_vectors,
        ids,
        matrix,
        projection=projection,
        min_cluster_size=min_cluster_size,
        min_samples=min_samples,
        n_neighbors=n_neighbors,
        min_dist=min_dist,
        n_representatives=n_representatives,
        axes=axes,
    )

    # One hydration for every id any part of the response names.
    wanted = {
        embedding_id for c in result.clusters for embedding_id in c.representatives
    }
    representatives = db.embeddings.hydrate(list(wanted))
    previews = db.embeddings.previews(ids, PREVIEW_CHARS)
    turns = db.embeddings.turns_for(
        list(representatives.values()), filter_params.filters
    )

    # What every plotted point is, beyond where clustering put it -- so the
    # scatter can be coloured by the guide or by language as well as by the
    # clustering.
    by_id = dict(zip(ids, coordinates))

    return EmbeddingClusterResponse(
        kind=kind,
        n_points=len(result.points),
        n_clusters=len(result.clusters),
        n_outliers=result.n_outliers,
        projection=result.projection,
        components=result.components,
        explained_variance_2d=result.explained_variance_2d,
        centered_by_question=center_by_question,
        centered_by_language=center_by_language,
        clusters=[
            EmbeddingCluster(
                id=cluster.id,
                size=cluster.size,
                question_purity=cluster.purity.get(GroupKind.QUESTION),
                language_purity=cluster.purity.get(GroupKind.LANGUAGE),
                representatives=[
                    EmbeddingSearchHit.from_hit(
                        representatives[embedding_id], 1.0, turns.get(embedding_id)
                    )
                    for embedding_id in cluster.representatives
                    if embedding_id in representatives
                ],
            )
            for cluster in result.clusters
        ],
        groups=_point_groups(
            db.session,
            project_id,
            [by_id[point.embedding_id] for point in result.points],
        ),
        points=[
            EmbeddingClusterPoint(
                id=point.embedding_id,
                cluster=point.cluster,
                probability=point.probability,
                x=point.x,
                y=point.y,
                preview=previews.get(point.embedding_id),
                section=by_id[point.embedding_id].section,
                main_question=by_id[point.embedding_id].main_question,
                sub_question=by_id[point.embedding_id].sub_question,
                language=by_id[point.embedding_id].language,
            )
            for point in result.points
        ],
    )


@router.get("/projects/{project_id}/analysis/embeddings/status")
async def get_embedding_status(
    project_id: UUID4,
    db: DBSession,
    jwt: ProjectViewer,
) -> EmbeddingStatus:
    """Coverage for this project, plus whether the embedding server is up.

    Coverage is what tells a client that an empty search means "nothing embedded
    yet" rather than "nothing matched".
    """
    settings = app_settings.services.embedding
    coverage = db.embeddings.coverage(project_id)

    return EmbeddingStatus(
        enabled=embedding_client.enabled,
        healthy=await embedding_client.health(),
        model=settings.model,
        dimension=settings.dimension,
        coverage=coverage,
        languages=db.embeddings.languages(project_id),
        total=sum(coverage.values()),
        queue_depth=chunk_queue.depth,
        queue_dropped=chunk_queue.dropped,
    )


@router.post("/projects/{project_id}/analysis/embeddings/backfill", status_code=202)
async def trigger_embedding_backfill(
    project_id: UUID4,
    db: DBSession,
    jwt: ProjectEditor,
) -> EmbeddingBackfillResponse:
    """Queue everything in this project that has no current vector.

    Deriving the chunks is database work and happens here, in the request;
    embedding them is minutes of work against the inference server and is left
    to the background worker, which already batches, retries and survives a
    server outage. Poll the status endpoint -- `queue_depth` falling to zero is
    what "done" looks like.

    Idempotent: a chunk whose text, model and format version are unchanged is
    never queued, so re-triggering while a run is in flight adds nothing.
    """
    if not embedding_client.enabled:
        raise HTTPException(503, detail="Embedding is not enabled on this deployment")

    outstanding, failed = pending_chunks(
        db, model=app_settings.services.embedding.model, project_id=project_id
    )

    queued = sum(chunk_queue.put(chunk) for chunk in outstanding)

    return EmbeddingBackfillResponse(
        queued=queued,
        # Non-zero when the queue filled up: the rest stay unembedded until the
        # next trigger or a CLI run, rather than being silently forgotten.
        skipped=len(outstanding) - queued,
        failed_interviews=[str(interview_id) for interview_id, _ in failed],
        queue_depth=chunk_queue.depth,
    )
