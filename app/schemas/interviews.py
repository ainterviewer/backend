"""Interviews, their messages and the interview list."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import UUID4, AfterValidator, Field, field_validator, model_validator

from ainterviewer.interfaces import SecurityIntervention
from ainterviewer.interview_guides import Image, InterviewGuide, SurveyItem
from ainterviewer.synthesize.interviewees import InterviewSubject
from ainterviewer.types import (
    Feedback,
    Interviewer,
    InterviewStatus,
    LanguageCode,
    MessageRole,
    MessageType,
    TestType,
)

from ..types import InterviewType
from .analysis import CodingPublic, MessageCommentPublic
from .common import PaginatedResponse, Schema
from .reports import MessageReportPublic


class InterviewBase(Schema):
    id: UUID4
    interview_guide: InterviewGuide | None
    language: LanguageCode = "EN"
    interviewer: Interviewer = Interviewer.AI
    status: InterviewStatus = InterviewStatus.INACTIVE
    type: InterviewType = InterviewType.DISTRIBUTED
    created_at: datetime
    last_updated: datetime | None = None
    total_time_spent: int = 0
    survey_token: str | None = None
    user_agent: str | None = None
    ip_address: str | None = None
    referer: str | None = None
    platform_version: str | None = None
    test_name: str | None = None
    #: The version of the project's content it runs; see InterviewTable.
    content_version_id: UUID4 | None = None


class InterviewPublic(InterviewBase):
    # Both default, so an interview can be returned without its transcript.
    # `n_messages` comes from the SQL expression on InterviewTable rather than
    # from `len(messages)`, so it stays correct when the messages are not
    # loaded -- see InterviewRepository.get_interview.
    n_messages: int = 0
    messages: list[MessagePublic] = []


class InterviewSummaryPublic(Schema):
    """One row of the interview list. Deliberately carries no messages: the
    list only ever renders `n_messages`, and the transcript is fetched from
    /interviews/{id}/messages when a single interview is opened."""

    id: UUID4
    language: LanguageCode = "EN"
    interviewer: Interviewer = Interviewer.AI
    status: InterviewStatus
    type: InterviewType
    created_at: datetime
    last_updated: datetime | None = None
    total_time_spent: int = 0
    n_messages: int
    #: How many of this interview's messages were flagged for review: questions
    #: the respondent reported, and answers the safety check intervened on.
    n_reports: int = 0
    #: How many times the safety check intervened, counted off the transcript
    #: -- test interviews included, which `n_reports` never flags.
    n_interventions: int = 0
    test_name: str | None = None
    # The participant this interview belongs to, joined in from the
    # participant record. NULL for interviews that were never distributed to
    # one, and for interviews whose participant has since been deleted.
    pid: str | None = None


class MessageBase(Schema):
    message_id: int
    content: str
    role: MessageRole
    interview_id: UUID4
    project_id: UUID4
    message_type: MessageType = MessageType.TEXT
    section: int | None = None
    main_question: int | None = None
    sub_question: int | None = None
    is_introduction: bool = False
    outro: bool = False
    timed: bool = False
    can_answer: bool = True
    include_in_history: bool = True
    attachment: Path | None = None
    audio_file: str | None = None
    feedback: Feedback | None = None
    created_at: datetime
    image: Image | list[Image] | None = None
    survey_item: SurveyItem | None = None
    skipped_by_condition: bool = False
    security_intervention: SecurityIntervention | None = None


class MessagePublic(MessageBase):
    id: UUID4
    codings: list[CodingPublic] = []
    comments: list[MessageCommentPublic] = []
    # Declared here, so every validation of a message reads the relationship:
    # eager-load it with the rest in `InterviewRepository._message_options`.
    reports: list[MessageReportPublic] = []

    @field_validator("comments", mode="after")
    @classmethod
    def _roots_only(
        cls, comments: list[MessageCommentPublic]
    ) -> list[MessageCommentPublic]:
        """The ORM relationship holds every comment on the message; the thread
        is exposed as roots carrying their own replies."""
        return [comment for comment in comments if comment.parent_id is None]

    interview_type: InterviewType


class IntervieweeBase(Schema):
    interview_id: UUID4
    interview_subject: InterviewSubject | str


class IntervieweeCreate(IntervieweeBase):
    pass


class IntervieweePublic(IntervieweeBase):
    id: UUID4


class InterviewFilterParams(Schema):
    """The interview list's filters, as the list endpoint takes them.

    Lets a bulk action target "everything matching" rather than ids the client
    has seen: with server-side paging it only ever holds one page of them.
    `get_interviews` takes this same model as its query parameters, so "all
    123 selected" means exactly the 123 the list counted.
    """

    interview_types: list[InterviewType] | None = None
    types: list[InterviewType] | None = None
    statuses: list[InterviewStatus] | None = None
    languages: list[str] | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None
    completed: bool | None = None
    search: str | None = Field(default=None, max_length=200)
    pid: str | None = Field(default=None, max_length=200)
    reported: bool | None = None
    intervened: bool | None = None


class InterviewSelection(Schema):
    """Interviews picked either by id or by the list's filters, never both."""

    interview_ids: list[UUID4] | None = None
    filters: InterviewFilterParams | None = None

    @model_validator(mode="after")
    def _exactly_one_selection(self):
        if (self.interview_ids is None) == (self.filters is None):
            raise ValueError("Give exactly one of interview_ids or filters.")
        return self


def _known_timezone(name: str | None) -> str | None:
    if name is None:
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"Unknown time zone: {name}")
    return name


class ExportMessagesRequest(InterviewSelection):
    """`csv` and `xlsx` export the message table, `docx` and `pdf` readable
    transcripts. `archive` makes it a ZIP with one file per interview."""

    format: Literal["csv", "xlsx", "docx", "pdf"] = "csv"
    archive: bool = False
    #: IANA name the transcript times are shown in, normally the browser's.
    #: The app's own time zone when absent.
    timezone: Annotated[str | None, AfterValidator(_known_timezone)] = None


class InterviewCreate(Schema):
    interviewer: Interviewer = Interviewer.AI
    interview_type: InterviewType = InterviewType.DISTRIBUTED
    test_run_id: UUID4 | None = None
    experiment_id: UUID4 | None = None
    synthetic_test_type: TestType | None = None
    participant_id: UUID4 | None = None
    pid: str | None = None

    # Extra context
    external_params: dict[str, Any] | None = Field(
        None,
        description="Extra query parameters, can include external user-id or the like.",
    )
    referer: str | None = Field(None, description="The referer of original request")


class MessageFeedbackRequest(Schema):
    """Feedback on one message of the caller's own interview.

    Carries no interview or project id on purpose: both come from the
    interview_token cookie, so a caller cannot leave feedback on somebody
    else's transcript by naming it. Mirrors SpeechRequest.
    """

    # Interview-scoped, so it only identifies a message together with the
    # interview the token names.
    message_id: int
    feedback: Feedback | None = None


class SpeechRequest(Schema):
    # The interview-scoped integer id of the message to synthesize. The text is
    # looked up server-side so participants can't synthesize arbitrary text.
    message_id: int


class FacetCount(Schema):
    """One selectable value of a filter, and how many rows carry it."""

    value: str
    count: int


class InterviewFacets(Schema):
    """The values each interview filter can usefully offer.

    Counted server-side because the client holds a single page: it has no way
    to know which statuses or languages exist across the whole result set, let
    alone how many rows each one accounts for.
    """

    status: list[FacetCount] = []
    language: list[FacetCount] = []
    type: list[FacetCount] = []
    #: "true"/"false" rather than a set of values: the filter behind it asks
    #: whether an interview has any reported questions at all.
    reported: list[FacetCount] = []
    #: Likewise: whether the safety check intervened in an interview at all.
    intervened: list[FacetCount] = []


class InterviewListResponse(PaginatedResponse[InterviewSummaryPublic]):
    """A page of interviews plus the filter options that fit the query."""

    facets: InterviewFacets = InterviewFacets()


class InterviewResumeLinkPublic(Schema):
    """The state of an interview's most recent resume link.

    Deliberately carries no token: the plaintext is shown once at creation and
    is not stored, so there is nothing here to show again. This only answers
    "is a link outstanding, and what happened to it".
    """

    created_at: datetime
    expires_at: datetime
    redeemed_at: datetime | None = None
    revoked_at: datetime | None = None
    # Precomputed rather than left to the client: "still usable" folds in the
    # clock, and a client with a skewed one would draw the wrong badge.
    redeemable: bool


class InterviewResumeLinkResponse(Schema):
    """A freshly minted resume link. The only time the URL ever exists."""

    url: str
    expires_at: datetime


class InterviewResumeResponse(Schema):
    """What the interview page needs to resume after redeeming a link.

    The credential itself rides back as the httponly ``interview_token``
    cookie; these ids only tell the page which interview to reconnect to.
    """

    project_id: UUID4
    interview_id: UUID4


class MessageFeedbackResponse(Schema):
    interview_id: UUID4
    project_id: UUID4
    message_id: int
    feedback: Feedback | None
