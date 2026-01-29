from typing import Generic, TypeVar

from pydantic import UUID4, BaseModel

from ainterviewer.types import Feedback

T = TypeVar("T")


class PaginatedResponse(BaseModel, Generic[T]):
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


class MessageFeedbackResponse(BaseModel):
    interview_id: UUID4
    project_id: UUID4
    message_id: int
    feedback: Feedback | None
