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
from ....dependencies import DBSession, UserToken

router = APIRouter()


def _require_modify_rights(db: DBSession, comment_id: UUID4, jwt: UserToken) -> None:
    try:
        allowed = db.analysis.can_modify_comment(comment_id, jwt.user_id, jwt.scope)
    except NoResultFound:
        raise HTTPException(404, detail="Comment not found")

    if not allowed:
        raise HTTPException(403, detail="Not allowed to modify this comment")


@router.get("/messages/{message_id}/comments")
async def get_message_comments(
    message_id: UUID4,
    db: DBSession,
    jwt: UserToken,
) -> list[MessageCommentPublic]:
    return db.analysis.get_message_comments(message_id)


@router.post("/messages/{message_id}/comments")
async def add_message_comment(
    message_id: UUID4,
    comment: MessageCommentCreate,
    db: DBSession,
    jwt: UserToken,
) -> MessageCommentPublic:
    try:
        return db.analysis.add_message_comment(message_id, jwt.user_id, comment)
    except CommentThreadError as error:
        raise HTTPException(400, detail=str(error))


@router.put("/analysis/comments/{comment_id}")
async def update_message_comment(
    comment_id: UUID4,
    comment: MessageCommentUpdate,
    db: DBSession,
    jwt: UserToken,
) -> MessageCommentPublic:
    _require_modify_rights(db, comment_id, jwt)
    return db.analysis.update_message_comment(comment_id, comment.body)


@router.delete("/analysis/comments/{comment_id}")
async def delete_message_comment(
    comment_id: UUID4,
    db: DBSession,
    jwt: UserToken,
):
    _require_modify_rights(db, comment_id, jwt)
    db.analysis.delete_message_comment(comment_id)
