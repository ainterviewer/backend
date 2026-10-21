from datetime import date, datetime

from pydantic import UUID4, BaseModel, computed_field

from ainterviewer.interview_guides import (
    LocalizedConsent,
    LocalizedInterviewGuide,
    LocalizedWelcome,
)
from ainterviewer.types import Feedback

from ..db.models import InterviewSummaryPublic, ParticipantPublic
from ..db.types import ContentPart
from ..types import Scope


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
    #: "true"/"false" rather than a set of values: the filter behind it asks
    #: whether an interview has any reported questions at all.
    reported: list[FacetCount] = []
    #: Likewise: whether the safety check intervened in an interview at all.
    intervened: list[FacetCount] = []


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


class DemoLimitDefaults(BaseModel):
    """The configured demo budgets (`app.demo_limits`): what a user without an
    override gets."""

    concurrent: int
    daily: int
    lifetime: int


class DemoBudgetItem(BaseModel):
    limit: int
    used: int

    @computed_field
    @property
    def remaining(self) -> int:
        return max(self.limit - self.used, 0)


class DemoBudget(BaseModel):
    """A demo user's synthetic-interview budgets and how much of each is spent.

    `limit` is the user's own (an override, else the configured default), so
    two demo users can see different numbers.
    """

    #: Interviews in flight at once.
    concurrent: DemoBudgetItem
    #: Interviews started in the last 24 hours.
    daily: DemoBudgetItem
    #: Interviews started ever.
    lifetime: DemoBudgetItem


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


class ProjectContentPublic(BaseModel):
    """A project's guide, consent and welcome with their text per language."""

    guide: LocalizedInterviewGuide
    consent: LocalizedConsent | None
    welcome: LocalizedWelcome | None
    #: Each part's revision, for its editor's next `If-Match`.
    revisions: dict[ContentPart, int]


#################
# Admin overview #
#################


class ScopeCounts(BaseModel):
    """A count split by account scope, so the overview can hide demo accounts
    (invited in batches that would swamp real growth) without a refetch."""

    admin: int = 0
    user: int = 0
    demo: int = 0


class InterviewTypeCounts(BaseModel):
    distributed: int = 0
    manual_test: int = 0
    synthetic_test: int = 0


class DailyStats(BaseModel):
    """One calendar day (app time zone) of the admin overview.

    `accounts` and `usable` are of accounts that still exist: a deleted user
    leaves no trace, so the history shrinks with them.
    """

    day: date
    signups: ScopeCounts
    # Accounts existing at the end of the day.
    accounts: ScopeCounts
    # Of those, the ones whose `expires_at` had not passed: what demo expiry
    # takes out of `accounts`.
    usable: ScopeCounts
    # Distinct users active that day, and over the 7 days ending on it.
    active: ScopeCounts
    weekly_active: ScopeCounts
    interviews: InterviewTypeCounts
    projects: int


class AdminStatsSummary(BaseModel):
    """Headline figures as of today, independent of the requested range."""

    accounts: ScopeCounts
    usable: ScopeCounts
    active_today: ScopeCounts
    active_7d: ScopeCounts
    active_30d: ScopeCounts
    new_7d: ScopeCounts
    new_30d: ScopeCounts


class RetentionWeek(BaseModel):
    """Of a cohort's `eligible` members -- those whose week `week` after
    signing up has fully passed -- how many were active during it."""

    week: int
    eligible: int
    retained: int


class RetentionCohort(BaseModel):
    """The users of one scope who signed up in one month (`YYYY-MM`)."""

    month: str
    scope: Scope
    size: int
    weeks: list[RetentionWeek]


class AdminStats(BaseModel):
    start: date
    end: date
    summary: AdminStatsSummary
    days: list[DailyStats]
    retention: list[RetentionCohort]
