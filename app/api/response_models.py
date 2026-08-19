from pydantic import UUID4, BaseModel

from ainterviewer.types import Feedback

from ..db.models import ParticipantPublic


class PaginatedResponse[T](BaseModel):
    total: int
    items: list[T]


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
