from pydantic import UUID4, BaseModel, Field, field_validator

from ainterviewer.synthesize.interviewees import BackgroundInfoOptions
from ainterviewer.types import LanguageCode


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
