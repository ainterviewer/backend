from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import UUID4
from sqlalchemy.exc import NoResultFound

from ....db.models import (
    AnalysisCategoryCreate,
    AnalysisCategoryPublic,
    FilteredMessagesRequest,
    MessageAnnotationCreate,
    MessageAnnotationPublic,
    MessagePublic,
)
from ....dependencies import (
    DBSession,
    ProjectAnnotator,
    ProjectEditor,
    ProjectViewer,
    UserToken,
)

router = APIRouter()


def _require_modify_rights(
    db: DBSession, project_id: UUID4, annotation_id: UUID4, jwt: UserToken
) -> None:
    """Its author, or somebody who moderates the project.

    The role check on the endpoint has already established that the caller is
    on this project at all -- which is why an annotation written by a
    collaborator who has since been removed is no longer theirs to edit. This
    decides who among the project's own people may act on another member's
    coding.
    """
    try:
        allowed = db.analysis.can_modify_annotation(
            project_id, annotation_id, jwt.user_id, jwt.scope
        )
    except NoResultFound:
        raise HTTPException(404, detail="Annotation not found")

    if not allowed:
        raise HTTPException(403, detail="Not allowed to modify this annotation")


@router.get("/projects/{project_id}/analysis/categories")
async def get_analysis_categories(
    project_id: UUID4,
    db: DBSession,
    jwt: UserToken,
    _: ProjectViewer,
) -> list[AnalysisCategoryPublic]:
    return db.analysis.get_analysis_categories(project_id)


@router.post("/projects/{project_id}/analysis/categories")
async def create_analysis_category(
    project_id: UUID4,
    category: AnalysisCategoryCreate,
    db: DBSession,
    jwt: UserToken,
    _: ProjectEditor,
):
    if project_id != category.project_id:
        raise HTTPException(400, detail="project_id mismatch between route and payload")

    return db.analysis.create_analysis_category(category)


@router.put("/projects/{project_id}/analysis/categories/{category_id}")
async def update_analysis_category(
    project_id: UUID4,
    category_id: UUID4,
    category: AnalysisCategoryCreate,
    db: DBSession,
    jwt: UserToken,
    _: ProjectEditor,
) -> AnalysisCategoryPublic:
    try:
        return db.analysis.update_analysis_category(project_id, category_id, category)
    except NoResultFound:
        raise HTTPException(404, detail="Category not found")


@router.delete("/projects/{project_id}/analysis/categories/{category_id}")
async def delete_analysis_category(
    project_id: UUID4,
    category_id: UUID4,
    db: DBSession,
    jwt: UserToken,
    _: ProjectEditor,
):
    try:
        db.analysis.delete_analysis_category(project_id, category_id)
    except NoResultFound:
        raise HTTPException(404, detail="Category not found")


@router.get("/projects/{project_id}/messages/{message_id}/annotations")
async def get_message_annotations(
    project_id: UUID4,
    message_id: UUID4,
    db: DBSession,
    jwt: UserToken,
    _: ProjectViewer,
) -> list[MessageAnnotationPublic]:
    try:
        return db.analysis.get_message_annotations(project_id, message_id)
    except NoResultFound:
        raise HTTPException(404, detail="Message not found")


@router.post("/projects/{project_id}/messages/{message_id}/annotations")
async def add_message_annotation(
    project_id: UUID4,
    message_id: UUID4,
    annotation: MessageAnnotationCreate,
    db: DBSession,
    jwt: UserToken,
    _: ProjectAnnotator,
) -> MessageAnnotationPublic:
    if annotation.user_id != jwt.user_id:
        raise HTTPException(400, detail="user_id mismatch between user and payload")

    if annotation.message_id != message_id:
        raise HTTPException(400, detail="message_id mismatch between route and payload")

    try:
        return db.analysis.add_message_annotation(project_id, annotation)
    except NoResultFound as error:
        raise HTTPException(404, detail=str(error))


@router.put("/projects/{project_id}/analysis/annotations/{annotation_id}")
async def update_message_annotation(
    project_id: UUID4,
    annotation_id: UUID4,
    annotation: MessageAnnotationCreate,
    db: DBSession,
    jwt: UserToken,
    _: ProjectAnnotator,
) -> MessageAnnotationPublic:
    if annotation.user_id != jwt.user_id:
        raise HTTPException(400, detail="user_id mismatch between user and payload")

    _require_modify_rights(db, project_id, annotation_id, jwt)
    try:
        return db.analysis.update_message_annotation(
            project_id, annotation_id, annotation
        )
    except NoResultFound as error:
        raise HTTPException(404, detail=str(error))


@router.delete("/projects/{project_id}/analysis/annotations/{annotation_id}")
async def delete_message_annotation(
    project_id: UUID4,
    annotation_id: UUID4,
    db: DBSession,
    jwt: UserToken,
    _: ProjectAnnotator,
):
    _require_modify_rights(db, project_id, annotation_id, jwt)
    db.analysis.delete_message_annotation(project_id, annotation_id)


@router.post("/analysis/{project_id}/messages/count")
async def get_filtered_messages_count(
    project_id: UUID4,
    filters: FilteredMessagesRequest,
    db: DBSession,
    jwt: UserToken,
    _: ProjectViewer,
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
    _: ProjectViewer,
    skip: Annotated[int, Query()] = 0,
    limit: Annotated[int, Query()] = 20,
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
        include_previous_on_user=filters.include_previous_on_user,
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
    _: ProjectViewer,
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
    _: ProjectViewer,
) -> list[MessagePublic]:
    return db.analysis.get_message_context(
        project_id,
        interview_id,
        message_id,
        context_after=True,
    )
