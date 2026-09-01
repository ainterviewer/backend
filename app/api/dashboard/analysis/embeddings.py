from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import UUID4
from sqlalchemy.exc import NoResultFound

from ainterviewer.types import EmbeddingKind, InterviewStatus

from ....db.models import (
    EmbeddingBackfillResponse,
    EmbeddingCluster,
    EmbeddingClusterPoint,
    EmbeddingClusterResponse,
    EmbeddingSearchHit,
    EmbeddingSearchResponse,
    EmbeddingSimilarResponse,
    EmbeddingStatus,
)
from ....db.repositories.embedding import EmbeddingFilters
from ....dependencies import DBSession, ProjectEditor, ProjectViewer
from ....embed.backfill import pending_chunks
from ....embed.client import EmbeddingUnavailable, embedding_client
from ....embed.clustering import (
    DEFAULT_MIN_CLUSTER_SIZE,
    cluster_vectors,
)
from ....embed.queue import chunk_queue
from ....embed.templates import QueryTask
from ....settings import app_settings

router = APIRouter()


class SearchFilterParams:
    """The non-vector half of a search, as a reusable dependency.

    Filtering happens in SQL before anything is scored, so these narrow the
    candidate set rather than the result list -- asking for 10 results from one
    participant returns 10 of theirs, not whichever of the global top 10
    happened to be theirs.
    """

    def __init__(
        self,
        language: str | None = None,
        status: InterviewStatus | None = None,
        participant_id: UUID4 | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        interview_id: Annotated[list[UUID4] | None, Query()] = None,
        include_synthetic: bool = False,
    ):
        self.filters = EmbeddingFilters(
            interview_ids=interview_id,
            language=language,
            status=status,
            participant_id=participant_id,
            created_after=created_after,
            created_before=created_before,
            include_synthetic=include_synthetic,
        )


@router.get("/projects/{project_id}/analysis/embeddings/search")
async def search_embeddings(
    project_id: UUID4,
    db: DBSession,
    jwt: ProjectViewer,
    filter_params: Annotated[SearchFilterParams, Depends()],
    query: Annotated[str, Query(min_length=1, max_length=2000)],
    kind: EmbeddingKind = EmbeddingKind.QA_PAIR,
    task: QueryTask = QueryTask.RETRIEVAL,
    k: Annotated[int, Query(ge=1, le=100)] = 10,
) -> EmbeddingSearchResponse:
    """Semantic search over one project's embedded interview text.

    `task` selects the instruction the *query* is templated under; the stored
    vectors are task-free, so switching tasks costs a query embedding and
    nothing else. `kind` picks the unit searched -- QA pairs by default, since a
    lone answer is often too short to mean anything out of context.
    """
    if not embedding_client.enabled:
        raise HTTPException(503, detail="Embedding is not enabled on this deployment")

    try:
        query_vector = await embedding_client.embed_query(query, task)
    except EmbeddingUnavailable as error:
        raise HTTPException(503, detail=f"Embedding server unavailable: {error}")

    try:
        hits = db.embeddings.search(
            project_id=project_id,
            query_vector=query_vector,
            kind=kind,
            k=k,
            filters=filter_params.filters,
        )
    except ValueError as error:
        # Raised when the query's dimension does not match what is stored,
        # i.e. the model changed and the corpus has not been re-embedded.
        raise HTTPException(409, detail=str(error))

    return EmbeddingSearchResponse(
        query=query,
        kind=kind,
        task=task,
        candidates=db.embeddings.count_candidates(
            project_id=project_id, kind=kind, filters=filter_params.filters
        ),
        items=[EmbeddingSearchHit.from_hit(hit.embedding, hit.score) for hit in hits],
    )


@router.get("/projects/{project_id}/analysis/embeddings/{embedding_id}/similar")
async def find_similar_embeddings(
    project_id: UUID4,
    embedding_id: UUID4,
    db: DBSession,
    jwt: ProjectViewer,
    filter_params: Annotated[SearchFilterParams, Depends()],
    k: Annotated[int, Query(ge=1, le=100)] = 10,
) -> EmbeddingSimilarResponse:
    """Chunks most like an existing one -- "more like this".

    Costs no inference: the query vector is the one already stored, so this
    works even when the embedding server is down. Searches within the source's
    own kind and never returns the source itself.
    """
    try:
        source, hits = db.embeddings.similar_to(
            embedding_id=embedding_id, k=k, filters=filter_params.filters
        )
    except NoResultFound:
        raise HTTPException(404, detail="Embedding not found")
    except ValueError as error:
        raise HTTPException(409, detail=str(error))

    if source.project_id != project_id:
        # The route is authorised on project_id, so a chunk from elsewhere must
        # not be reachable through it.
        raise HTTPException(404, detail="Embedding not found")

    return EmbeddingSimilarResponse(
        source=EmbeddingSearchHit.from_hit(source, 1.0),
        candidates=db.embeddings.count_candidates(
            project_id=project_id, kind=source.kind, filters=filter_params.filters
        ),
        items=[EmbeddingSearchHit.from_hit(hit.embedding, hit.score) for hit in hits],
    )


PREVIEW_CHARS = 240


@router.get("/projects/{project_id}/analysis/embeddings/clusters")
async def cluster_embeddings(
    project_id: UUID4,
    db: DBSession,
    jwt: ProjectViewer,
    filter_params: Annotated[SearchFilterParams, Depends()],
    kind: EmbeddingKind = EmbeddingKind.QA_PAIR,
    min_cluster_size: Annotated[int, Query(ge=2, le=500)] = DEFAULT_MIN_CLUSTER_SIZE,
    min_samples: Annotated[int | None, Query(ge=1, le=500)] = None,
    center_by_question: bool = False,
    n_representatives: Annotated[int, Query(ge=1, le=10)] = 3,
) -> EmbeddingClusterResponse:
    """Cluster a project's chunks and project them to 2D.

    HDBSCAN over the first 50 principal components, with the scatter taken from
    the first two of that same projection -- so the picture is a sub-projection
    of the space the clusters were found in, not a separate fit. HDBSCAN rather
    than k-means because exploratory work does not know `k` up front, and
    because points it cannot place come back as outliers instead of being forced
    into the nearest blob.

    **Read `question_purity` before reading the clusters.** A QA-pair chunk
    repeats its interview question verbatim, and every respondent was asked the
    same one, so uncentred clustering tends to recover the interview guide
    rather than what anyone said. `center_by_question=true` subtracts each
    question's mean vector first, which removes that shared component and costs
    no re-embedding.

    Computed per request -- milliseconds at this corpus size -- so
    `min_cluster_size` is an interactive control, not a migration.
    """
    ids, matrix, groups = db.embeddings.vectors_for(
        project_id=project_id, kind=kind, filters=filter_params.filters
    )

    result = cluster_vectors(
        ids,
        matrix,
        min_cluster_size=min_cluster_size,
        min_samples=min_samples,
        n_representatives=n_representatives,
        group_keys=groups,
        center_by_group=center_by_question,
    )

    # One hydration for every id any part of the response names.
    wanted = {
        embedding_id for c in result.clusters for embedding_id in c.representatives
    }
    representatives = db.embeddings.hydrate(list(wanted))
    previews = db.embeddings.previews(ids, PREVIEW_CHARS)

    return EmbeddingClusterResponse(
        kind=kind,
        n_points=len(result.points),
        n_clusters=len(result.clusters),
        n_outliers=result.n_outliers,
        components=result.components,
        explained_variance_2d=result.explained_variance_2d,
        centered_by_question=center_by_question,
        clusters=[
            EmbeddingCluster(
                id=cluster.id,
                size=cluster.size,
                question_purity=cluster.question_purity,
                representatives=[
                    EmbeddingSearchHit.from_hit(representatives[embedding_id], 1.0)
                    for embedding_id in cluster.representatives
                    if embedding_id in representatives
                ],
            )
            for cluster in result.clusters
        ],
        points=[
            EmbeddingClusterPoint(
                id=point.embedding_id,
                cluster=point.cluster,
                probability=point.probability,
                x=point.x,
                y=point.y,
                preview=previews.get(point.embedding_id),
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
