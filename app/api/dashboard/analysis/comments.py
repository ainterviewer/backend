"""Threaded discussion on interview messages.

Several project members can comment on the same message and answer each other.
Threads are two levels deep: a root comment plus a flat list of replies.

Authorship comes from the caller's token, never from the payload. Editing and
deleting are the author's, plus whoever moderates the project -- see
`AnalysisRepository.can_modify_comment`.
"""

from fastapi import APIRouter, HTTPException
from pydantic import UUID4
from sqlalchemy.exc import NoResultFound

from ....db.models import (
    MessageCommentCreate,
    MessageCommentPublic,
    MessageCommentUpdate,
)
from ....db.repositories.errors import CommentThreadError
from ....dependencies import (
    DBSession,
    ProjectAnnotator,
    ProjectViewer,
    UserToken,
)

router = APIRouter()


def _require_modify_rights(
    db: DBSession, project_id: UUID4, comment_id: UUID4, jwt: UserToken
) -> None:
    try:
        allowed = db.analysis.can_modify_comment(
            project_id, comment_id, jwt.user_id, jwt.scope
        )
    except NoResultFound:
        raise HTTPException(404, detail="Comment not found")

    if not allowed:
        raise HTTPException(403, detail="Not allowed to modify this comment")


@router.get("/projects/{project_id}/messages/{message_id}/comments")
async def get_message_comments(
    project_id: UUID4,
    message_id: UUID4,
    db: DBSession,
    jwt: UserToken,
    _: ProjectViewer,
) -> list[MessageCommentPublic]:
    try:
        return db.analysis.get_message_comments(project_id, message_id)
    except NoResultFound:
        raise HTTPException(404, detail="Message not found")


@router.post("/projects/{project_id}/messages/{message_id}/comments")
async def add_message_comment(
    project_id: UUID4,
    message_id: UUID4,
    comment: MessageCommentCreate,
    db: DBSession,
    jwt: UserToken,
    _: ProjectAnnotator,
) -> MessageCommentPublic:
    try:
        return db.analysis.add_message_comment(
            project_id, message_id, jwt.user_id, comment
        )
    except NoResultFound:
        raise HTTPException(404, detail="Message not found")
    except CommentThreadError as error:
        raise HTTPException(400, detail=str(error))


@router.put("/projects/{project_id}/analysis/comments/{comment_id}")
async def update_message_comment(
    project_id: UUID4,
    comment_id: UUID4,
    comment: MessageCommentUpdate,
    db: DBSession,
    jwt: UserToken,
    _: ProjectAnnotator,
) -> MessageCommentPublic:
    _require_modify_rights(db, project_id, comment_id, jwt)
    return db.analysis.update_message_comment(project_id, comment_id, comment.body)


@router.delete("/projects/{project_id}/analysis/comments/{comment_id}")
async def delete_message_comment(
    project_id: UUID4,
    comment_id: UUID4,
    db: DBSession,
    jwt: UserToken,
    _: ProjectAnnotator,
):
    _require_modify_rights(db, project_id, comment_id, jwt)
    db.analysis.delete_message_comment(project_id, comment_id)
