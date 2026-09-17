"""Every reported question on the platform, in one queue.

The platform's own review, across all projects: a question a respondent found
offensive is a safety matter for whoever runs the platform, and waiting for
each project's members to notice their own queue is not a review.

This writes the admin track only -- `admin_status` and its resolver. A project
member resolving a report in their own queue leaves these rows exactly as they
are, and an admin resolving here leaves theirs; see `MessageReportTable`.
"""

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import UUID4, BaseModel

from ...db.models import MessageReportRowPublic
from ...db.types import ReportStatus
from ...dependencies import AdminToken, DBSession

router = APIRouter(prefix="/reports")


class AdminReportReviewRequest(BaseModel):
    ids: list[UUID4]
    status: ReportStatus


class AdminReportReadRequest(BaseModel):
    ids: list[UUID4]


class AdminReportReviewResponse(BaseModel):
    updated: int


@router.get("")
async def get_reports(
    db: DBSession,
    jwt: AdminToken,
    statuses: Annotated[list[ReportStatus] | None, Query()] = None,
    unread_only: Annotated[bool, Query()] = False,
) -> list[MessageReportRowPublic]:
    """Every report there is, newest first, with the question it is about.

    `statuses` filters on the admin track, not the project's: this queue is
    about what the platform has reviewed. Unfiltered by default so that the
    page can count and facet the whole set client-side, as the other admin
    tables do.
    """
    return db.reports.list_reports(
        user_id=jwt.user_id,
        track="admin",
        statuses=statuses,
        unread_only=unread_only,
    )


@router.post("/resolve")
async def resolve_reports(
    review: AdminReportReviewRequest,
    db: DBSession,
    jwt: AdminToken,
) -> AdminReportReviewResponse:
    """Resolve, dismiss or reopen reports on the platform track."""
    return AdminReportReviewResponse(
        updated=db.reports.resolve(
            user_id=jwt.user_id,
            report_ids=review.ids,
            status=review.status,
            track="admin",
        )
    )


@router.post("/read")
async def mark_reports_read(
    read: AdminReportReadRequest,
    db: DBSession,
    jwt: AdminToken,
) -> AdminReportReviewResponse:
    """Record that this admin has seen these reports."""
    return AdminReportReviewResponse(
        updated=db.reports.mark_read(user_id=jwt.user_id, report_ids=read.ids)
    )
