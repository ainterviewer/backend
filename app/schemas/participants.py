"""A project's participants and the emails sent to them."""

from __future__ import annotations

from datetime import datetime

from pydantic import MISSING, UUID4, EmailStr

from ainterviewer.types import InterviewStatus, LanguageCode

from .common import Schema


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


class DeleteParticipantsRequest(Schema):
    participant_ids: list[UUID4]


class ExportParticipantsRequest(Schema):
    participant_ids: list[UUID4] | None = None


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
