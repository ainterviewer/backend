"""The codebook, and the codings made with it.

The codebook is read and written whole (`GET`/`PUT .../codebook`), because it
is edited whole: one drag re-parents a branch and reseats two sets of
siblings. Codes carry client-supplied ids, so a save is a diff and a code that
survives keeps its codings -- see `AnalysisRepository.save_codebook`.

A coding is one application of one code to one passage, by one coder. Editing
and deleting one is the author's, plus whoever moderates the project.
"""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import UUID4
from sqlalchemy.exc import NoResultFound

from ....db.models import (
    CodebookPublic,
    CodebookPut,
    CodingCreate,
    CodingPublic,
    CodingsForMessages,
    FilteredMessagesRequest,
    MessagePublic,
)
from ....db.repositories.errors import CodebookError, CodingError
from ....dependencies import (
    DBSession,
    ProjectAnnotator,
    ProjectEditor,
    ProjectViewer,
    UserToken,
)

router = APIRouter()


def _require_modify_rights(
    db: DBSession, project_id: UUID4, coding_id: UUID4, jwt: UserToken
) -> None:
    """Its author, or somebody who moderates the project.

    The role check on the endpoint has already established that the caller is
    on this project at all -- which is why a coding made by a collaborator who
    has since been removed is no longer theirs to edit. This decides who among
    the project's own people may act on another member's coding.
    """
    try:
        allowed = db.analysis.can_modify_coding(
            project_id, coding_id, jwt.user_id, jwt.scope
        )
    except NoResultFound:
        raise HTTPException(404, detail="Coding not found")

    if not allowed:
        raise HTTPException(403, detail="Not allowed to modify this coding")


@router.get("/projects/{project_id}/analysis/codebook")
async def get_codebook(
    project_id: UUID4,
    db: DBSession,
    jwt: UserToken,
    _: ProjectViewer,
) -> CodebookPublic:
    return db.analysis.get_codebook(project_id)


@router.put("/projects/{project_id}/analysis/codebook")
async def save_codebook(
    project_id: UUID4,
    codebook: CodebookPut,
    db: DBSession,
    jwt: UserToken,
    _: ProjectEditor,
) -> CodebookPublic:
    try:
        return db.analysis.save_codebook(project_id, codebook)
    except CodebookError as error:
        raise HTTPException(400, detail=str(error))


@router.get("/projects/{project_id}/analysis/codebook/counts")
async def get_code_counts(
    project_id: UUID4,
    db: DBSession,
    jwt: UserToken,
    _: ProjectViewer,
) -> dict[UUID4, int]:
    """How many passages carry each code. Codes with none are left out."""
    return db.analysis.count_codings_by_code(project_id)


@router.get("/projects/{project_id}/messages/{message_id}/codings")
async def get_message_codings(
    project_id: UUID4,
    message_id: UUID4,
    db: DBSession,
    jwt: UserToken,
    _: ProjectViewer,
) -> list[CodingPublic]:
    try:
        return db.analysis.get_message_codings(project_id, message_id)
    except NoResultFound:
        raise HTTPException(404, detail="Message not found")


@router.post("/projects/{project_id}/analysis/codings/by-message")
async def get_codings_for_messages(
    project_id: UUID4,
    lookup: CodingsForMessages,
    db: DBSession,
    jwt: UserToken,
    _: ProjectViewer,
) -> dict[UUID4, list[CodingPublic]]:
    """The codings on a set of messages, keyed by message id.

    A POST for a read, like the filtered-message endpoints below: explore draws
    a page of results as turns from many interviews at once, and the ids of
    those turns are more than a query string should carry. Messages with no
    codings are left out.
    """
    return db.analysis.get_codings_for_messages(project_id, lookup.message_ids)


@router.post("/projects/{project_id}/messages/{message_id}/codings")
async def add_message_coding(
    project_id: UUID4,
    message_id: UUID4,
    coding: CodingCreate,
    db: DBSession,
    jwt: UserToken,
    _: ProjectAnnotator,
) -> CodingPublic:
    """Code a passage. The coder is the caller, never the payload."""
    try:
        return db.analysis.add_message_coding(
            project_id, message_id, jwt.user_id, coding
        )
    except NoResultFound as error:
        raise HTTPException(404, detail=str(error))
    except CodingError as error:
        raise HTTPException(400, detail=str(error))


@router.put("/projects/{project_id}/analysis/codings/{coding_id}")
async def update_message_coding(
    project_id: UUID4,
    coding_id: UUID4,
    coding: CodingCreate,
    db: DBSession,
    jwt: UserToken,
    _: ProjectAnnotator,
) -> CodingPublic:
    _require_modify_rights(db, project_id, coding_id, jwt)
    try:
        return db.analysis.update_message_coding(project_id, coding_id, coding)
    except NoResultFound as error:
        raise HTTPException(404, detail=str(error))
    except CodingError as error:
        raise HTTPException(400, detail=str(error))


@router.delete("/projects/{project_id}/analysis/codings/{coding_id}")
async def delete_message_coding(
    project_id: UUID4,
    coding_id: UUID4,
    db: DBSession,
    jwt: UserToken,
    _: ProjectAnnotator,
):
    _require_modify_rights(db, project_id, coding_id, jwt)
    db.analysis.delete_message_coding(project_id, coding_id)


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
        code_ids=filters.code_ids,
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
        code_ids=filters.code_ids,
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
