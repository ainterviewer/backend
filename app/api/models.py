from typing import Optional

from pydantic import UUID4, BaseModel, Field, field_validator, model_validator

from ainterviewer.synthesize.interviewees import BackgroundInfoOptions
from ainterviewer.types import Feedback, LanguageCode


class ServerUpdate(BaseModel):
    activate: set[str] = Field(default_factory=set)
    deactivate: set[str] = Field(default_factory=set)

    @model_validator(mode="after")
    def check_activations(self):
        if self.activate and self.deactivate:
            if self.activate - self.deactivate:
                raise ValueError(
                    "Cannot activate and deactivate the same server at the same time"
                )
        if self.activate is None and self.deactivate is None:
            raise ValueError("Either 'activate' or 'deactivate' must be provided")

        return self


class MessageFeedback(BaseModel):
    interview_id: UUID4
    project_id: UUID4
    message_id: int
    feedback: Optional[Feedback]


class Broadcast(BaseModel):
    message: str


class SynthesizeRequest(BaseModel):
    answering_model: str | None = None
    n_interviews: int = Field(ge=1, le=100)
    language: LanguageCode = "EN"
    delay_before_answers: tuple[float, float] | None = None


class SynthesizeResponse(BaseModel):
    project_id: UUID4
    message: str
    status: str


class UpdateBackgroundInfoRequest(BaseModel):
    background_info: BackgroundInfoOptions


class UpdateFixedAnswersRequest(BaseModel):
    answers: list[str]

    @field_validator("answers")
    @classmethod
    def validate_answers(cls, value):
        return [answer.strip() for answer in value]


class TestSetupRequest(BaseModel):
    project_id: UUID4
    test_id: UUID4
