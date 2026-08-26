from datetime import datetime

from pydantic import UUID4, BaseModel

from ainterviewer.types import Feedback

from ..db.models import InterviewSummaryPublic, ParticipantPublic


class PaginatedResponse[T](BaseModel):
    total: int
    items: list[T]


class FacetCount(BaseModel):
    """One selectable value of a filter, and how many rows carry it."""

    value: str
    count: int


class InterviewFacets(BaseModel):
    """The values each interview filter can usefully offer.

    Counted server-side because the client holds a single page: it has no way
    to know which statuses or languages exist across the whole result set, let
    alone how many rows each one accounts for.
    """

    status: list[FacetCount] = []
    language: list[FacetCount] = []
    type: list[FacetCount] = []


class InterviewListResponse(PaginatedResponse[InterviewSummaryPublic]):
    """A page of interviews plus the filter options that fit the query."""

    facets: InterviewFacets = InterviewFacets()


class InterviewResumeLinkPublic(BaseModel):
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


class InterviewResumeLinkCreated(BaseModel):
    """A freshly minted resume link. The only time the URL ever exists."""

    url: str
    expires_at: datetime


class InterviewResumeRedeemed(BaseModel):
    """What the interview page needs to resume after redeeming a link.

    The credential itself rides back as the httponly ``interview_token``
    cookie; these ids only tell the page which interview to reconnect to.
    """

    project_id: UUID4
    interview_id: UUID4


class ErrorResponse(BaseModel):
    detail: str


class MediaUploadResponse(BaseModel):
    message: str
    filename: str


class SynthesizeResponse(BaseModel):
    project_id: UUID4
    message: str
    status: str


class SendParticipantEmailResponse(BaseModel):
    sent: list[UUID4]
    skipped: list[UUID4]


class UploadParticipantsResponse(BaseModel):
    participants: list[ParticipantPublic]
    skipped_rows: int


class ParticipantEmailAttachment(BaseModel):
    filename: str
    size: int
    content_type: str | None = None


class MessageFeedbackResponse(BaseModel):
    interview_id: UUID4
    project_id: UUID4
    message_id: int
    feedback: Feedback | None


class ProbingPromptPreview(BaseModel):
    """Rendered probing-agent prompts with the project's editable slots injected.

    Interview-time context (transcript, framing, etc.) is shown as labelled
    placeholders since those values only exist while an interview is running.
    """

    system: str
    instruction: str
