from typing import Literal

from fastapi import Query
from pydantic import UUID4, BaseModel

from ainterviewer.agents.prompts.models import PromptTemplates
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
