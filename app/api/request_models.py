from typing import Literal

from fastapi import Query
from pydantic import UUID4, BaseModel, Field, field_validator

from ainterviewer.agents.prompts.models import PromptTemplates
from ainterviewer.synthesize.interviewees import BackgroundInfoOptions
from ainterviewer.types import Interviewer, LanguageCode, TestType

from ..db.types import InterviewType
from ..types import ProjectStatus


class PaginatedQueryParams(BaseModel):
    offset: int = Query(default=0, le=100)
    limit: int = Query(default=20, le=100)
    column: str = Query("created_at", description="Column to sort by")
    order: Literal["asc", "desc"] = Query("desc", description="Sorting order")


class PromptRequest(BaseModel):
    prompt: str


class BroadcastRequest(BaseModel):
    message: str


class InterviewGuideGenerationRequest(PromptRequest): ...


class QuestionSectionGenerationRequest(PromptRequest): ...


class QuestionGenerationRequest(PromptRequest):
    section_idx: int


class ProjectStatusChangeRequest(BaseModel):
    status: ProjectStatus


class ProjectTitleUpdateRequest(BaseModel):
    title: str


class PromptsUpdateRequest(BaseModel):
    probing_agent: PromptTemplates


class CreateProjectRequest(BaseModel):
    title: str
    default_language: LanguageCode


class LoginData(BaseModel):
    email: str
    password: str
    extended: bool = False


class CreateInterviewRequest(BaseModel):
    interviewer: Interviewer = Interviewer.AI
    interview_type: InterviewType = InterviewType.DISTRIBUTED
    test_run_id: UUID4 | None = None
    experiment_id: UUID4 | None = None
    synthetic_test_type: TestType | None = None


class SynthesizeRequest(BaseModel):
    answering_model: str | None = None
    n_interviews: int = Field(ge=1, le=100)
    language: LanguageCode = "EN"
    delay_before_answers: tuple[float, float] | None = None


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
