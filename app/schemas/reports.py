"""Messages reported by respondents or flagged by the safety check."""

from __future__ import annotations

from datetime import datetime

from pydantic import UUID4, Field, field_validator

from ainterviewer.types import LanguageCode

from ..db.types import (
    InterviewType,
    ReportReason,
    ReportSource,
    ReportStatus,
    SafetyFlag,
)
from .common import Schema


class MessageReportCreate(Schema):
    """A respondent reporting one question of their own interview.

    Carries no interview or project id: both come from the `interview_token`
    cookie, so a respondent cannot report into somebody else's transcript by
    naming it. Mirrors `MessageFeedbackRequest`.
    """

    #: Interview-scoped, so it only identifies a message together with the
    #: interview the token names. The row uuid is resolved server-side.
    message_id: int
    reason: ReportReason
    comment: str | None = Field(default=None, max_length=2000)

    @field_validator("comment", mode="after")
    @classmethod
    def _blank_is_none(cls, comment: str | None) -> str | None:
        """An empty textarea is no comment, not a comment of "".

        The dialog submits its comment field whether or not the respondent
        typed in it, so without this a whitespace-only string would be stored
        and the reviewer's queue would show a report that appears to carry a
        note and does not.
        """
        if comment is None:
            return None
        return comment.strip() or None


class MessageReportPublic(Schema):
    """A report as the project sees it.

    Carries the project's own review track and **not** the platform's. The
    admin track is the platform's internal moderation record: whether a
    reviewer there judged a question offensive or waved it through is not a
    project member's business, and it is left off this model rather than
    merely hidden in the UI -- a column can be hidden while the value still
    sits in the JSON.

    `MessageReportAdminPublic` is the wider view, served only to the platform
    queue. The respondent is not named because there is nothing to name: they
    hold no account, and the interview the report hangs off already says whose
    it was.
    """

    id: UUID4
    message_id: UUID4
    interview_id: UUID4
    project_id: UUID4
    source: ReportSource = ReportSource.RESPONDENT
    #: Set when `source` is the respondent.
    reason: ReportReason | None = None
    comment: str | None = None
    #: Set when `source` is the safety check. Viewers of the project see it,
    #: as they see the transcript it was raised on.
    safety: SafetyFlag | None = None
    created_at: datetime
    updated_at: datetime

    status: ReportStatus
    resolved_by_id: UUID4 | None = None
    resolved_at: datetime | None = None


class MessageReportAdminPublic(MessageReportPublic):
    """A report including the platform's own review track.

    Served only where the caller is a platform admin. Everything project-side
    uses the narrower `MessageReportPublic`.
    """

    admin_status: ReportStatus
    admin_resolved_by_id: UUID4 | None = None
    admin_resolved_at: datetime | None = None


class MessageReportRowPublic(MessageReportPublic):
    """A report with the context a reviewer needs to judge it.

    The question's wording, the project it belongs to and the respondent's pid
    are joined in rather than left to the client: a queue of report ids is not
    something anyone can review, and a reviewer holding one page cannot resolve
    a message id against a transcript they have not opened.

    `read_by_me` is the calling reviewer's own read state, not a property of
    the report -- a project member and a platform admin looking at the same
    report each see their own.
    """

    #: The interview-scoped number of the reported message, as the transcript
    #: shows it. `message_id` above is the row uuid.
    question_number: int
    #: The flagged message's text: the interviewer's question for a
    #: respondent report, the respondent's answer for a safety flag.
    question: str
    project_title: str
    language: LanguageCode = "EN"
    #: Whether the flag came from a real respondent or from someone testing
    #: the project -- the two call for very different reviews.
    interview_type: InterviewType = InterviewType.DISTRIBUTED
    pid: str | None = None
    read_by_me: bool = False


class MessageReportAdminRowPublic(MessageReportRowPublic):
    """A queue row for the platform's own review.

    The admin track is added back here, and only here: the project queue is
    served `MessageReportRowPublic`, which has no field for it.
    """

    admin_status: ReportStatus
    admin_resolved_by_id: UUID4 | None = None
    admin_resolved_at: datetime | None = None


class ReportReviewRequest(Schema):
    """Set one track's status on a batch of reports."""

    ids: list[UUID4]
    status: ReportStatus


class ReportReadRequest(Schema):
    ids: list[UUID4]


class ReportReviewResponse(Schema):
    """How many rows the call actually changed.

    Not an error when it is fewer than were asked for: on the project track an
    id belonging to another project is dropped rather than refused, so a stale
    client cannot learn from the response whether a report it should not see
    exists.
    """

    updated: int
