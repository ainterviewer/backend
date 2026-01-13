from zmq.decorators import context
from fastapi import APIRouter, HTTPException
from pydantic import UUID4

from ....db.models import (
    AnalysisCategoryCreate,
    AnalysisCategoryPublic,
    FilteredMessagesRequest,
    MessageAnnotationCreate,
    MessageAnnotationPublic,
    MessagePublic,
)
from ....dependencies import DBSession, UserToken

router = APIRouter()


@router.get("/projects/{project_id}/analysis/categories")
async def get_analysis_categories(
    project_id: UUID4,
    db: DBSession,
    jwt: UserToken,
) -> list[AnalysisCategoryPublic]:
    return db.analysis.get_analysis_categories(project_id)


@router.post("/projects/{project_id}/analysis/categories")
async def create_analysis_category(
    project_id: UUID4,
    category: AnalysisCategoryCreate,
    db: DBSession,
    jwt: UserToken,
):
    if project_id != category.project_id:
        raise HTTPException(400, detail="project_id mismatch between route and payload")

    return db.analysis.create_analysis_category(category)


@router.put("/analysis/categories/{category_id}")
async def update_analysis_category(
    category_id: UUID4,
    category: AnalysisCategoryCreate,
    db: DBSession,
    jwt: UserToken,
) -> AnalysisCategoryPublic:
    return db.analysis.update_analysis_category(category_id, category)


@router.delete("/analysis/categories/{category_id}")
async def delete_analysis_category(
    category_id: UUID4,
    db: DBSession,
    jwt: UserToken,
):
    db.analysis.delete_analysis_category(category_id)


@router.get("/messages/{message_id}/annotations")
async def get_message_annotations(
    message_id: UUID4,
    db: DBSession,
    jwt: UserToken,
) -> list[MessageAnnotationPublic]:
    return db.analysis.get_message_annotations(message_id)


@router.post("/messages/{message_id}/annotations")
async def add_message_annotation(
    message_id: UUID4,
    annotation: MessageAnnotationCreate,
    db: DBSession,
    jwt: UserToken,
) -> MessageAnnotationPublic:
    if annotation.user_id != jwt.user_id:
        raise HTTPException(400, detail="user_id mismatch between user and payload")

    if annotation.message_id != message_id:
        raise HTTPException(400, detail="message_id mismatch between route and payload")

    return db.analysis.add_message_annotation(annotation)


@router.put("/analysis/annotations/{annotation_id}")
async def update_message_annotation(
    annotation_id: UUID4,
    annotation: MessageAnnotationCreate,
    db: DBSession,
    jwt: UserToken,
) -> MessageAnnotationPublic:
    if annotation.user_id != jwt.user_id:
        raise HTTPException(400, detail="user_id mismatch between user and payload")

    return db.analysis.update_message_annotation(annotation_id, annotation)


@router.delete("/analysis/annotations/{annotation_id}")
async def delete_message_annotation(
    annotation_id: UUID4,
    db: DBSession,
    jwt: UserToken,
):
    db.analysis.delete_message_annotation(annotation_id)


@router.post("/analysis/{project_id}/messages/count")
async def get_filtered_messages_count(
    project_id: UUID4,
    filters: FilteredMessagesRequest,
    db: DBSession,
    jwt: UserToken,
) -> int:
    return db.analysis.count_filtered_messages(
        project_id,
        category_ids=filters.category_ids,
        search_text=filters.search_text,
        exact_match=filters.exact_match,
        case_sensitive=filters.case_sensitive,
        questions=filters.questions,
    )


@router.post("/analysis/{project_id}/messages")
async def get_filtered_messages(
    project_id: UUID4,
    filters: FilteredMessagesRequest,
    db: DBSession,
    jwt: UserToken,
    skip: int = 0,
    limit: int = 20,
) -> list[MessagePublic]:
    return db.analysis.get_filtered_messages(
        project_id,
        skip,
        limit,
        category_ids=filters.category_ids,
        search_text=filters.search_text,
        exact_match=filters.exact_match,
        case_sensitive=filters.case_sensitive,
        questions=filters.questions,
    )


@router.post(
    "/analysis/{project_id}/interviews/{interview_id}/messages/{message_id}/context_before"
)
async def get_message_context_before(
    project_id: UUID4,
    interview_id: UUID4,
    message_id: UUID4,
    db: DBSession,
    jwt: UserToken,
) -> list[MessagePublic]:
    return db.analysis.get_message_context(
        project_id,
        interview_id,
        message_id,
        context_before=True,
    )


@router.post(
    "/analysis/{project_id}/interviews/{interview_id}/messages/{message_id}/context_after"
)
async def get_message_context_after(
    project_id: UUID4,
    interview_id: UUID4,
    message_id: UUID4,
    db: DBSession,
    jwt: UserToken,
) -> list[MessagePublic]:
    return db.analysis.get_message_context(
        project_id,
        interview_id,
        message_id,
        context_after=True,
    )
