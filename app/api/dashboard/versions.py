"""Versions of a project's content: the history, publishing and restore.

See `VersionRepository` for what a version is, and app/db/versioning.py for
how a publish is numbered.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import UUID4
from sqlalchemy.exc import NoResultFound

from ...collab.rooms import content_rooms
from ...db.versioning import Bump
from ...dependencies import (
    DBSession,
    DemoToken,
    IfMatch,
    ProjectEditor,
    ProjectViewer,
    set_etag,
)
from ...schemas.common import PaginatedQueryParams, PaginatedResponse
from ...schemas.content import (
    ContentStatus,
    ContentVersionPublic,
    ContentVersionSummary,
    SaveVersionRequest,
)

router = APIRouter(tags=["versions"])


@router.get("/projects/{project_id}/versions/status")
async def get_content_status(
    project_id: UUID4,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectViewer,
    response: Response,
) -> ContentStatus:
    """Whether the draft differs from what respondents get, what publishing
    it would be numbered, and what blocks publishing."""
    # The draft as editors see it: an open guide room saves first.
    await content_rooms.flush(project_id)
    status = db.versions.status(project_id)
    set_etag(response, status.revision)
    return status


@router.get("/projects/{project_id}/versions")
async def list_content_versions(
    project_id: UUID4,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectViewer,
    params: Annotated[PaginatedQueryParams, Depends()],
) -> PaginatedResponse[ContentVersionSummary]:
    """The history, newest first."""
    versions, total = db.versions.list_versions(
        project_id, limit=params.limit, offset=params.offset
    )
    return PaginatedResponse(items=versions, total=total)


@router.get("/projects/{project_id}/versions/{version_id}")
async def get_content_version(
    project_id: UUID4,
    version_id: UUID4,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectViewer,
) -> ContentVersionPublic:
    """A version with its content, for previewing it."""
    try:
        return db.versions.get_version(project_id, version_id)
    except NoResultFound as exc:
        raise HTTPException(status_code=404, detail="No such version") from exc


@router.post("/projects/{project_id}/versions")
async def save_content_version(
    project_id: UUID4,
    request: SaveVersionRequest,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectEditor,
    revision: IfMatch,
) -> ContentVersionSummary:
    """Save the draft as a version, and publish it if asked.

    `If-Match` is the revision the caller is looking at: publishing what
    someone else changed since would release work nobody reviewed (412).
    Publishing is refused (409) while a language lacks text, or for a bump
    lower than the changes need.
    """
    # So what is published is what editors see. Anything it saves that the
    # caller has not seen moves the revision, and is a 412 below.
    await content_rooms.flush(project_id)
    db.versions.check_revision(project_id, revision)
    if request.publish:
        return db.versions.publish(
            project_id,
            jwt.user_id,
            request.label,
            None if request.bump is None else Bump.named(request.bump),
        )
    return db.versions.save_version(project_id, jwt.user_id, request.label)


@router.post("/projects/{project_id}/versions/{version_id}/restore")
async def restore_content_version(
    project_id: UUID4,
    version_id: UUID4,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectEditor,
    revision: IfMatch,
    response: Response,
) -> ContentStatus:
    """Replace the draft with a version, keeping the draft it replaces in
    the history. Publishes nothing. The new revision comes back as `ETag`."""
    # The draft it keeps as a `restore` version is what editors see.
    await content_rooms.flush(project_id)
    db.versions.check_revision(project_id, revision)
    try:
        db.versions.restore(project_id, version_id, jwt.user_id)
    except NoResultFound as exc:
        raise HTTPException(status_code=404, detail="No such version") from exc
    status = db.versions.status(project_id)
    set_etag(response, status.revision)
    return status
