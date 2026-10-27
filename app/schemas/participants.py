"""A project's participants and the emails sent to them."""

from __future__ import annotations

from datetime import datetime

from pydantic import MISSING, UUID4, EmailStr, Field, model_validator

from ainterviewer.types import InterviewStatus, LanguageCode

from .common import PaginatedResponse, Schema
from .interviews import FacetCount


class ParticipantBase(Schema):
    name: str | None = None
    email: EmailStr | None = None
    pid: str | None = None
    participating: bool = True
    lang: LanguageCode | None = None


class ParticipantCreate(ParticipantBase):
    pass


class ParticipantUpdate(Schema):
    name: str | None | MISSING = MISSING
    email: EmailStr | None | MISSING = MISSING
    pid: str | MISSING = MISSING
    participating: bool | MISSING = MISSING


class ParticipantPublic(ParticipantBase):
    id: UUID4
    project_id: UUID4
    participant_id: UUID4
    folder_id: UUID4
    created_at: datetime
    pid: str  # ty:ignore[invalid-mutable-override]
    latest_interview_at: datetime | None = None
    latest_interview_status: InterviewStatus | None = None


class ParticipantFilterParams(Schema):
    """The participant list's filters, as the list endpoint takes them.

    Shared with the bulk actions so that "all 1,234 selected" means exactly
    the rows the list counted -- with server-side paging the client only ever
    holds one page of their ids.
    """

    search: str | None = Field(default=None, max_length=200)
    participating: bool | None = None
    #: The status of each participant's most recent interview. Participants
    #: with no interview have none, and match no status.
    statuses: list[InterviewStatus] | None = None


class ParticipantSelection(Schema):
    """Participants picked either by id or by the list's filters, never both."""

    participant_ids: list[UUID4] | None = None
    filters: ParticipantFilterParams | None = None

    @model_validator(mode="after")
    def _exactly_one_selection(self):
        if (self.participant_ids is None) == (self.filters is None):
            raise ValueError("Give exactly one of participant_ids or filters.")
        return self


class ParticipantFacets(Schema):
    """The values each participant filter can usefully offer, counted over the
    whole result set rather than the page the client holds."""

    #: "true"/"false": the filter behind it is a boolean.
    participating: list[FacetCount] = []
    status: list[FacetCount] = []


class ParticipantListResponse(PaginatedResponse[ParticipantPublic]):
    """A page of participants plus the filter options that fit the query."""

    facets: ParticipantFacets = ParticipantFacets()


class ParticipantEmailTemplateRequest(Schema):
    subject: str | None = None
    template: str | None = None


class SendParticipantEmailRequest(Schema):
    # If None, send to every participating participant with an email address.
    participant_ids: list[UUID4] | None = None


class SendParticipantEmailResponse(Schema):
    sent: list[UUID4]
    skipped: list[UUID4]


class ParticipantUploadResponse(Schema):
    participants: list[ParticipantPublic]
    skipped_rows: int


class ParticipantEmailAttachmentPublic(Schema):
    filename: str
    size: int
    content_type: str | None = None
