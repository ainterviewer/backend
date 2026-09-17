"""The project's own queue of questions its respondents reported.

The reviewer here is a project member, and every route names the project it
acts in so the role check has something to check -- the report ids arrive in
the request body, and a role on the project in the path says nothing about
them, which is why the repository scopes each write by it as well.

The platform admin's queue is `app.api.admin.reports`: the same rows, the
other status. Neither track can clear the other's; see `MessageReportTable`.
"""

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import UUID4, BaseModel

from ...db.models import MessageReportRowPublic
from ...db.types import ReportStatus
from ...dependencies import DBSession, ProjectAnnotator, ProjectViewer, UserToken

router = APIRouter(tags=["reports"])


class ReportReviewRequest(BaseModel):
    """Set one track's status on a batch of reports."""

    ids: list[UUID4]
    status: ReportStatus


class ReportReadRequest(BaseModel):
    ids: list[UUID4]


class ReportReviewResponse(BaseModel):
    """How many rows the call actually changed.

    Not an error when it is fewer than were asked for: an id belonging to
    another project is dropped rather than refused, so a stale client cannot
    learn from the response whether a report it should not see exists.
    """

    updated: int


@router.get("/projects/{project_id}/reports")
async def get_project_reports(
    project_id: UUID4,
    db: DBSession,
    jwt: UserToken,
    _: ProjectViewer,
    statuses: Annotated[list[ReportStatus] | None, Query()] = None,
    unread_only: Annotated[bool, Query()] = False,
) -> list[MessageReportRowPublic]:
    """Every question reported in this project, newest first.

    Read access is enough to see them: a report is part of what the interview
    produced, and a member who may read the transcript may read the objection
    to one of its questions.
    """
    return db.reports.list_reports(
        user_id=jwt.user_id,
        project_id=project_id,
        track="owner",
        statuses=statuses,
        unread_only=unread_only,
    )


@router.post("/projects/{project_id}/reports/resolve")
async def resolve_project_reports(
    project_id: UUID4,
    review: ReportReviewRequest,
    db: DBSession,
    jwt: UserToken,
    _: ProjectAnnotator,
) -> ReportReviewResponse:
    """Resolve, dismiss or reopen reports on this project's questions.

    Writes the project track only. Annotator rather than viewer: clearing a
    report is a judgement about the interview guide, not a read of it.
    """
    return ReportReviewResponse(
        updated=db.reports.resolve(
            user_id=jwt.user_id,
            report_ids=review.ids,
            status=review.status,
            track="owner",
            project_id=project_id,
        )
    )


@router.post("/projects/{project_id}/reports/read")
async def mark_project_reports_read(
    project_id: UUID4,
    read: ReportReadRequest,
    db: DBSession,
    jwt: UserToken,
    _: ProjectViewer,
) -> ReportReviewResponse:
    """Record that this member has seen these reports.

    Viewer, unlike resolving: "I have read this" is a fact about the reader.
    """
    return ReportReviewResponse(
        updated=db.reports.mark_read(
            user_id=jwt.user_id,
            report_ids=read.ids,
            project_id=project_id,
        )
    )
