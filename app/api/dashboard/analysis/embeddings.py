import re
from collections.abc import Iterable
from dataclasses import replace
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import UUID4
from sqlalchemy.exc import NoResultFound
from starlette.concurrency import run_in_threadpool

from ainterviewer.constants import LANGUAGES
from ainterviewer.interview_guides import SurveyItem
from ainterviewer.interview_guides.survey_items import CheckboxItem
from ainterviewer.types import EmbeddingKind

from ....db.code_lookup import CodeIndex, resolve_all
from ....db.keyword_query import MARKUP_PATTERN, Scope, parse
from ....db.repositories.embedding import (
    DEFAULT_BROWSE_GROUPING,
    DEFAULT_BROWSE_ORDER,
    BrowseGrouping,
    BrowseOrder,
    ChunkCoordinates,
    CodeCoverage,
    EmbeddingFilters,
)
from ....db.repositories.project import render_default_guide
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
    matching_interviews,
    normalize_answer,
    options_of,
    values_of,
)
from ....dependencies import (
    DBSession,
    ProjectEditor,
    ProjectViewer,
    require_preview_features,
)
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
from ....schemas.embeddings import (
    CodeFacet,
    CodeFacets,
    EmbeddingBackfillResponse,
    EmbeddingBrowseResponse,
    EmbeddingCluster,
    EmbeddingClusterPoint,
    EmbeddingClusterResponse,
    EmbeddingCodeSimilarResponse,
    EmbeddingFilterParams,
    EmbeddingGroup,
    EmbeddingSearchHit,
    EmbeddingSearchResponse,
    EmbeddingSimilarResponse,
    EmbeddingStatus,
    InterviewTranscript,
    SearchPageParams,
    SurveyFacet,
    SurveyFacets,
    SurveyFacetValue,
)
from ....settings import app_settings
from ....types import GroupKind, Projection
from ...errors import INVALID_KEYWORD_QUERY

# The whole explore surface is a preview feature; see `require_preview_features`.
router = APIRouter(dependencies=[Depends(require_preview_features)])


def embedding_filters(
    project_id: UUID4,
    db: DBSession,
    params: Annotated[EmbeddingFilterParams, Query()],
) -> EmbeddingFilters:
    """The filters a search asked for, parsed and checked against the project.

    A dependency rather than a validator on the model because checking the
    `code:` terms of a keyword query takes the project's codebook.
    """
    return EmbeddingFilters(
        interview_ids=params.interview_id,
        languages=params.language,
        status=params.status,
        participant_id=params.participant_id,
        created_after=params.created_after,
        created_before=params.created_before,
        include_synthetic=params.include_synthetic,
        questions=_parse_questions(params.question),
        keyword=_checked_keyword(params.keyword, db, project_id),
        keyword_scope=params.keyword_scope,
        survey=_parse_survey(params.survey, params.survey_range),
        coded_mine=params.coded_mine,
        coded_others=params.coded_others,
        coder_join=params.coder_join,
        coder_id=params.coder_id,
        main_answers_only=params.main_answers_only,
    )


def _parse_questions(raw: list[str] | None) -> list[tuple[int, int]] | None:
    """`["0,2", "1,0"]` to `[(0, 2), (1, 0)]`, or 400 saying which one was bad.

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
                400,
                detail=(
                    f"question {value!r} is not a 'section,main_question' pair, "
                    "e.g. '0,2'"
                ),
            )
        try:
            section, main_question = (int(part) for part in parts)
        except ValueError:
            raise HTTPException(
                400,
                detail=(
                    f"question {value!r} is not a 'section,main_question' pair "
                    "of whole numbers, e.g. '0,2'"
                ),
            ) from None
        if section < 0 or main_question < 0:
            raise HTTPException(
                400,
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
    """Split `"0,2=option:1"` into `((0, 2), "option:1")`, or 400 saying why not.

    The coordinate is separated from the value by the first `=`, which no guide
    coordinate can contain -- so a value holding one, as a write-in easily
    might, still arrives whole.
    """
    coordinate, separator, rest = value.partition("=")
    if not separator:
        raise HTTPException(
            400,
            detail=(
                f"{param} {value!r} is not a 'section,main_question=value' "
                "selection, e.g. '0,2=option:1'"
            ),
        )
    pairs = _parse_questions([coordinate])
    if not pairs:
        raise HTTPException(400, detail=f"{param} {value!r} names no question")
    return pairs[0], rest


def _parse_survey_values(raw: list[str] | None) -> dict[Coordinate, tuple[Value, ...]]:
    """`["0,2=option:1", "0,2=text:kayaking"]` to the values chosen per item."""
    chosen: dict[Coordinate, list[Value]] = {}
    for entry in raw or []:
        coordinate, rest = _survey_coordinate("survey", entry)
        kind, separator, body = rest.partition(":")
        if not separator or kind not in {"option", "text"}:
            raise HTTPException(
                400,
                detail=(
                    f"survey value {rest!r} is neither 'option:<n>' nor 'text:<answer>'"
                ),
            )

        if kind == "option":
            try:
                position = int(body)
            except ValueError:
                raise HTTPException(
                    400, detail=f"survey value {rest!r} has a non-numeric option"
                ) from None
            if position < 0:
                raise HTTPException(
                    400,
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
                400,
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


def _checked_keyword(raw: str | None, db: DBSession, project_id: UUID4) -> str | None:
    """The keyword query, parsed here so a bad one is a 400 and not a 500.

    Parsed and thrown away rather than passed on as a tree: the repository takes
    a string and parses it itself, so that a caller which never touches this
    endpoint -- a script, a test -- gets the same language. Parsing twice costs
    nothing next to the scan, and it buys an error raised where FastAPI can turn
    it into a response.

    Any `code:` reference is placed in the project's codebook here too, and for
    the same reason. It is the half of reading a query that needs a project:
    whether `code:stress` names anything, and whether it names only one thing,
    cannot be known by a parser. Left to the repository it would surface as a
    500 from inside a scan; left out altogether it would be a filter that
    quietly matched nothing, which reads as "nobody was coded that way" rather
    than as "there is no such code".

    A `KeywordQueryError` from either answers 400 with the `position` of the
    offending character, so the client can point at it.
    """
    if raw is None or not raw.strip():
        return raw

    node = parse(raw)
    if node is not None:
        # `db` is the repository facade; the lookup wants the session it
        # wraps, which is the same one every repository on it shares.
        resolve_all(db.session, project_id, node)
    return raw


@router.get(
    "/projects/{project_id}/analysis/embeddings/search", responses=INVALID_KEYWORD_QUERY
)
async def search_embeddings(
    project_id: UUID4,
    db: DBSession,
    jwt: ProjectViewer,
    filters: Annotated[EmbeddingFilters, Depends(embedding_filters)],
    page: Annotated[SearchPageParams, Query()],
    query: Annotated[str, Query(min_length=1, max_length=2000)],
    kind: EmbeddingKind = EmbeddingKind.QA_PAIR,
    task: QueryTask = QueryTask.RETRIEVAL,
    whole_interviews: bool = False,
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

    `whole_interviews` sends interview-unit hits whole rather than as the
    six-turn window a card normally gets -- see the browse endpoint, which is
    where a list of interviews usually comes from.
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
            filters=filters,
        )
    except ValueError as error:
        # Raised when the query's dimension does not match what is stored,
        # i.e. the model changed and the corpus has not been re-embedded.
        raise HTTPException(409, detail=str(error))

    turns = db.embeddings.turns_for(
        [hit.embedding for hit in result.hits],
        filters,
        whole_interviews=whole_interviews,
    )
    numbers = db.embeddings.interview_numbers(project_id)

    return EmbeddingSearchResponse(
        query=query,
        kind=kind,
        task=task,
        candidates=result.scored,
        total=result.total,
        interviews=result.interviews,
        offset=page.offset,
        items=[
            EmbeddingSearchHit.from_hit(
                hit.embedding,
                hit.score,
                turns.get(hit.embedding.id),
                numbers.get(hit.embedding.interview_id),
            )
            for hit in result.hits
        ],
    )


@router.get(
    "/projects/{project_id}/analysis/embeddings/browse", responses=INVALID_KEYWORD_QUERY
)
async def browse_embeddings(
    project_id: UUID4,
    db: DBSession,
    jwt: ProjectViewer,
    filters: Annotated[EmbeddingFilters, Depends(embedding_filters)],
    page: Annotated[SearchPageParams, Query()],
    kind: EmbeddingKind = EmbeddingKind.QA_PAIR,
    order: BrowseOrder = DEFAULT_BROWSE_ORDER,
    group_by: BrowseGrouping = DEFAULT_BROWSE_GROUPING,
    seed: Annotated[str, Query(max_length=64, pattern=r"^[A-Za-z0-9_-]*$")] = "",
    whole_interviews: bool = False,
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

    `order` defaults to `random`, which is a claim about reading rather than
    about data: a researcher reads the top of this list far more carefully than
    the bottom, so a fixed order means the same interviews are always the
    closely-read ones. `seed` is what makes a shuffle survive paging -- the
    same seed is the same order, so page two continues page one -- and a client
    that draws a fresh seed per page load gets a fresh shuffle per visit.
    Ignored by the other orders, which need no seed to be stable.

    `group_by` is the other half of that ordering and changes what the page is a
    page *of*: `interview` runs one conversation at a time, `guide` runs one
    question at a time with every respondent's answer to it together, which is
    the shape a cross-interview reading wants. Nothing leaves either way, and
    `order` still decides whose answer comes first inside a block. Ignored under
    the interview unit, which has no coordinates to block by.

    `whole_interviews` sends interview-unit hits as whole transcripts rather
    than as the six-turn window a card normally gets. A list of interviews is a
    list of transcripts, and a window onto each of ten of them is ten openings;
    the cost is the whole corpus on the wire a page at a time, so it is asked
    for rather than assumed.
    """
    result = db.embeddings.browse(
        project_id=project_id,
        kind=kind,
        filters=filters,
        limit=page.limit,
        offset=page.offset,
        order=order,
        seed=seed,
        group_by=group_by,
    )

    turns = db.embeddings.turns_for(
        result.units, filters, whole_interviews=whole_interviews
    )
    numbers = db.embeddings.interview_numbers(project_id)

    return EmbeddingBrowseResponse(
        kind=kind,
        total=result.total,
        interviews=result.interviews,
        offset=page.offset,
        items=[
            EmbeddingSearchHit.from_hit(
                unit, None, turns.get(unit.id), numbers.get(unit.interview_id)
            )
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


def _facet_cohorts(
    session,
    project_id: UUID,
    coordinates: Iterable[Coordinate],
    survey: SurveyFilter | None,
    scope: set[UUID],
    include_synthetic: bool,
) -> dict[Coordinate, set[UUID]]:
    """Which interviews each item is counted over.

    Every filter applies except the item's own selection. That exception is the
    whole design: counted with it, choosing "Male" would take every other
    option in the same item to zero and leave the reader inside a selection
    they can no longer see out of -- the count that would tell them what
    widening to "Non-binary" costs is exactly the one their current choice has
    erased. Counted without it, the gender item still reads 56/62/3/1 over the
    completed interviews, and the *age* item reads the ages of completed men.

    One resolved set per selected item, intersected per facet, rather than one
    query per facet: the selections are a handful and the sets are small.
    """
    if survey is None or not survey:
        return {coordinate: scope for coordinate in coordinates}

    each: dict[Coordinate, set[UUID]] = {}
    for selected in survey.coordinates:
        one = SurveyFilter(
            values={selected: survey.values[selected]}
            if selected in survey.values
            else {},
            ranges={selected: survey.ranges[selected]}
            if selected in survey.ranges
            else {},
        )
        each[selected] = matching_interviews(
            session, project_id, one, include_synthetic=include_synthetic
        )

    cohorts: dict[Coordinate, set[UUID]] = {}
    for coordinate in coordinates:
        cohort = set(scope)
        for selected, matched in each.items():
            if selected != coordinate:
                cohort &= matched
        cohorts[coordinate] = cohort
    return cohorts


def survey_facets(
    session,
    project_id: UUID,
    include_synthetic: bool,
    scope: set[UUID] | None = None,
    survey: SurveyFilter | None = None,
) -> SurveyFacets:
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

    `scope` is the interviews the view's other filters leave, and `survey` the
    cohort filter currently applied; together they decide what each item is
    counted over -- see `_facet_cohorts`. The *items* and their options are
    still drawn from the whole project, so a filter never removes the control
    that would undo it: an option nobody in the cohort chose reads zero rather
    than disappearing.
    """
    rows = answer_rows(session, project_id, include_synthetic=include_synthetic)

    by_coordinate: dict[Coordinate, list] = {}
    for row in rows:
        by_coordinate.setdefault(row.coordinate, []).append(row)

    # The current draft, for the authored wording and the option labels. The
    # interviews ran against per-interview snapshots, so this is what the
    # questions are *called* and not what they were.
    guide = render_default_guide(session, project_id)

    authored: dict[Coordinate, tuple[str, SurveyItem | None]] = {}
    if guide is not None:
        for section_index, section in enumerate(guide.question_sections):
            for question_index, question in enumerate(section.questions):
                authored[(section_index, question_index)] = (
                    question.main_question,
                    question.survey_item,
                )

    cohorts = _facet_cohorts(
        session,
        project_id,
        by_coordinate,
        survey,
        {row.interview_id for row in rows} if scope is None else scope,
        include_synthetic,
    )

    items: list[SurveyFacet] = []
    for coordinate in sorted(by_coordinate):
        offered = by_coordinate[coordinate]
        # The item is described from every answer ever given to it and counted
        # over the cohort only: what the question *is* does not depend on who
        # is currently being looked at.
        cohort = cohorts[coordinate]
        here = [row for row in offered if row.interview_id in cohort]
        observed = offered[0].item
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


@router.get(
    "/projects/{project_id}/analysis/embeddings/survey-facets",
    responses=INVALID_KEYWORD_QUERY,
)
async def read_survey_facets(
    project_id: UUID4,
    db: DBSession,
    jwt: ProjectViewer,
    filters: Annotated[EmbeddingFilters, Depends(embedding_filters)],
) -> SurveyFacets:
    """What the cohort filter in the explore view is built from.

    The *items and their options* come from the whole project, always: a filter
    must never remove the control that would undo it, so an option nobody left
    in view chose reads zero rather than disappearing, and the language filter
    is offered from the whole corpus for the same reason.

    The *counts* are of the cohort the view is currently showing -- the
    interview filters, and the selections made in the other survey items. A
    count that ignored them said 62 male interviews beside a list drawn from 52,
    which reads as a bug in one of the two numbers rather than as two different
    questions. Each item is exempt from its own selection; `_facet_cohorts` has
    why.

    The keyword and question filters are deliberately not applied: they choose
    chunks, not people, and "interviews holding a matching chunk" is a
    different unit than the one every other number here is counted in.
    """
    filters = replace(filters, keyword=None, questions=None)
    # The scope is built *without* the survey filter, and the survey filter is
    # handed over separately: an item is exempt from its own selection, and a
    # scope that had already applied every selection would leave nothing for
    # that exemption to give back.
    scope = db.embeddings.interviews_in_scope(project_id, replace(filters, survey=None))

    return await run_in_threadpool(
        survey_facets,
        db.session,
        project_id,
        filters.include_synthetic,
        scope,
        filters.survey,
    )


def code_facets(
    index: CodeIndex, coverage: CodeCoverage, kind: EmbeddingKind
) -> CodeFacets:
    """The coverage arranged as one number per code, plus one per branch.

    A plain function rather than the endpoint itself, so the arithmetic that
    turns chunk sets into badges can be read against a codebook in a test.

    The branch total is the *union* of its codes' chunks and not the sum down
    it: an answer coded both "Wellbeing/Strain" and its parent is one answer,
    and adding the rows up would report it twice -- which is how a branch ends
    up claiming more chunks than the corpus holds.
    """
    items = []
    for code_id, subtree in index.subtrees().items():
        branch: set[tuple] = set()
        for descendant in subtree:
            branch |= coverage.units.get(descendant, set())
        if not branch:
            # Nothing on it and nothing under it, so there is no number to
            # send. A code the client does not hear about reads zero, which is
            # what it is, and the alternative is the whole codebook coming back
            # on every filter change to say nothing.
            continue
        items.append(
            CodeFacet(
                code_id=code_id,
                count=len(coverage.units.get(code_id, set())),
                subtree=len(branch),
            )
        )
    return CodeFacets(kind=kind, total=coverage.total, items=items)


@router.get(
    "/projects/{project_id}/analysis/embeddings/code-facets",
    responses=INVALID_KEYWORD_QUERY,
)
async def read_code_facets(
    project_id: UUID4,
    db: DBSession,
    jwt: ProjectViewer,
    filters: Annotated[EmbeddingFilters, Depends(embedding_filters)],
    kind: EmbeddingKind = EmbeddingKind.QA_PAIR,
) -> CodeFacets:
    """How much of what is on screen each code accounts for.

    The badge beside a code row, and the number that decides whether clicking
    *filter* on it is worth doing. Counted over the corpus the view is
    currently showing, so it answers "how much of *this*" rather than "how much
    of the project" -- the same choice the cohort filter's tallies make, and for
    the same reason: two numbers counted over different corpora next to each
    other read as a bug in one of them.

    Every filter applies **except the code terms in the query itself**. A count
    that applied them would take every other code to zero the moment one was
    chosen, leaving the reader inside a selection with nothing on screen
    offering a way out of it. `without_code_terms` is how, and why pruning
    rather than substitution.

    `kind` is the unit, and has to be the one the list is showing: one coding
    is one message, one question group and one interview at once, so a number
    without its unit is three different numbers.

    The counts and the filter can still disagree in one corner: under a
    coverage axis, a grouped chunk is counted here if it carries the code
    anywhere in it, where filtering asks the coverage question of the coded
    rows alone. It takes a coverage axis *and* a code filter together to see
    it, and the alternative -- one query per code -- costs a codebook of round
    trips to close a gap nobody is standing in.
    """
    coverage = await run_in_threadpool(
        db.embeddings.code_coverage,
        project_id=project_id,
        kind=kind,
        filters=filters,
    )
    index = CodeIndex.for_project(db.session, project_id)
    return code_facets(index, coverage, kind)


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
    validate it, so a query that cannot be read is a 400 here too rather than a
    transcript quietly rendered with nothing marked.
    """
    try:
        turns = db.embeddings.transcript(
            project_id=project_id,
            interview_id=interview_id,
            keyword=_checked_keyword(keyword, db, project_id),
            keyword_scope=keyword_scope,
        )
    except NoResultFound:
        raise HTTPException(404, detail="Interview not found")

    return InterviewTranscript(interview_id=interview_id, turns=turns)


@router.get(
    "/projects/{project_id}/analysis/embeddings/codes/{code_id}/similar",
    responses=INVALID_KEYWORD_QUERY,
)
async def find_embeddings_like_code(
    project_id: UUID4,
    code_id: UUID4,
    db: DBSession,
    jwt: ProjectViewer,
    filters: Annotated[EmbeddingFilters, Depends(embedding_filters)],
    page: Annotated[SearchPageParams, Query()],
    kind: EmbeddingKind = EmbeddingKind.QA_PAIR,
    subtree: bool = False,
    whole_interviews: bool = False,
) -> EmbeddingCodeSimilarResponse:
    """Chunks most like the ones a code has been applied to -- "find more like
    these".

    The other half of searching from a code, and the half that only exists once
    there is coded data. *Searching by definition* asks what the code says it
    is and needs nothing but the words; this asks what it has *become* in the
    hands of whoever applied it, which on a code used fifty times is a
    different and usually better query. Neither replaces the other, and a code
    nobody has used yet can only be asked the first.

    Costs no inference: every vector is already stored, so it works with the
    embedding server down.

    The already-coded chunks are in the results, near the top by construction.
    Left there rather than hidden: whether they sit together is the one thing
    this ranking says about the code *itself*, and a seed ranking low is a
    passage somebody coded loosely. A reader who wants only where the code has
    not reached writes `-code:x` in the keyword query, which is precise about
    which code it means and composes with every other filter.

    `subtree` averages the branch instead of the one code, which is what
    `code:x/*` selects. A GROUP is never applied on its own, so asking one
    without it is a query with no seeds -- a 409 saying so.
    """
    # Resolved either way, so that a code from another project is a 404 rather
    # than a centroid with no seeds wearing a 409.
    index = CodeIndex.for_project(db.session, project_id)
    branch = index.ids_under(code_id)
    if not branch:
        raise HTTPException(404, detail="Code not found")
    ids = branch if subtree else (code_id,)

    try:
        seeds, result = await run_in_threadpool(
            db.embeddings.like_code,
            project_id=project_id,
            code_ids=ids,
            kind=kind,
            limit=page.limit,
            offset=page.offset,
            filters=filters,
        )
    except ValueError as error:
        raise HTTPException(409, detail=str(error))

    turns = db.embeddings.turns_for(
        [hit.embedding for hit in result.hits],
        filters,
        whole_interviews=whole_interviews,
    )
    numbers = db.embeddings.interview_numbers(project_id)

    return EmbeddingCodeSimilarResponse(
        code_id=code_id,
        seeds=seeds,
        candidates=result.scored,
        total=result.total,
        interviews=result.interviews,
        offset=page.offset,
        items=[
            EmbeddingSearchHit.from_hit(
                hit.embedding,
                hit.score,
                turns.get(hit.embedding.id),
                numbers.get(hit.embedding.interview_id),
            )
            for hit in result.hits
        ],
    )


@router.get(
    "/projects/{project_id}/analysis/embeddings/{embedding_id}/similar",
    responses=INVALID_KEYWORD_QUERY,
)
async def find_similar_embeddings(
    project_id: UUID4,
    embedding_id: UUID4,
    db: DBSession,
    jwt: ProjectViewer,
    filters: Annotated[EmbeddingFilters, Depends(embedding_filters)],
    page: Annotated[SearchPageParams, Query()],
    whole_interviews: bool = False,
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
            filters=filters,
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
        [source, *(hit.embedding for hit in result.hits)],
        filters,
        whole_interviews=whole_interviews,
    )
    numbers = db.embeddings.interview_numbers(project_id)

    return EmbeddingSimilarResponse(
        source=EmbeddingSearchHit.from_hit(
            source, 1.0, turns.get(source.id), numbers.get(source.interview_id)
        ),
        candidates=result.scored,
        total=result.total,
        interviews=result.interviews,
        offset=page.offset,
        items=[
            EmbeddingSearchHit.from_hit(
                hit.embedding,
                hit.score,
                turns.get(hit.embedding.id),
                numbers.get(hit.embedding.interview_id),
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

    guide = render_default_guide(session, project_id)

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


@router.get(
    "/projects/{project_id}/analysis/embeddings/clusters",
    responses=INVALID_KEYWORD_QUERY,
)
async def cluster_embeddings(
    project_id: UUID4,
    db: DBSession,
    jwt: ProjectViewer,
    filters: Annotated[EmbeddingFilters, Depends(embedding_filters)],
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
        project_id=project_id, kind=kind, filters=filters
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
    turns = db.embeddings.turns_for(list(representatives.values()), filters)
    numbers = db.embeddings.interview_numbers(project_id)

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
                        representatives[embedding_id],
                        1.0,
                        turns.get(embedding_id),
                        numbers.get(representatives[embedding_id].interview_id),
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
        live=embedding_client.live,
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
