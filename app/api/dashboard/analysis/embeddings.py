from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import UUID4
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from starlette.concurrency import run_in_threadpool

from ainterviewer.constants import LANGUAGES
from ainterviewer.types import EmbeddingKind, InterviewStatus

from ....db.models import (
    EmbeddingBackfillResponse,
    EmbeddingCluster,
    EmbeddingClusterPoint,
    EmbeddingClusterResponse,
    EmbeddingGroup,
    EmbeddingSearchHit,
    EmbeddingSearchResponse,
    EmbeddingSimilarResponse,
    EmbeddingStatus,
)
from ....db.repositories.embedding import ChunkCoordinates, EmbeddingFilters
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

    turns = db.embeddings.turns_for([hit.embedding for hit in result.hits])

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

    turns = db.embeddings.turns_for([source, *(hit.embedding for hit in result.hits)])

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
    turns = db.embeddings.turns_for(list(representatives.values()))

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
