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
from pydantic import UUID4

from ...dependencies import DBSession, ProjectAnnotator, ProjectViewer, UserToken
from ...schemas.reports import (
    MessageReportRowPublic,
    ReportFilterParams,
    ReportReadRequest,
    ReportReviewRequest,
    ReportReviewResponse,
)

router = APIRouter(tags=["reports"])


@router.get("/projects/{project_id}/reports")
async def get_project_reports(
    project_id: UUID4,
    db: DBSession,
    jwt: UserToken,
    _: ProjectViewer,
    filters: Annotated[ReportFilterParams, Query()],
) -> list[MessageReportRowPublic]:
    """Every question reported in this project, newest first.

    Read access is enough to see them: a report is part of what the interview
    produced, and a member who may read the transcript may read the objection
    to one of its questions.
    """
    return db.reports.list_reports(
        user_id=jwt.user_id,
        project_id=project_id,
        statuses=filters.statuses,
        unread_only=filters.unread_only,
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
