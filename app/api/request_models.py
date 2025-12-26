from typing import Literal

from fastapi import Query
from pydantic import UUID4, BaseModel

from ainterviewer.agents.prompts.models import PromptTemplates
from ainterviewer.types import LanguageCode

from ..types import ProjectStatus


class PaginatedQueryParams(BaseModel):
    offset: int = Query(default=0, le=100)
    limit: int = Query(default=20, le=100)
    column: str = Query("created_at", description="Column to sort by")
    order: Literal["asc", "desc"] = Query("desc", description="Sorting order")


class InterviewGuideGenerationPromptRequest(BaseModel):
    prompt: str


class ProjectStatusChangeRequest(BaseModel):
    status: ProjectStatus


class ProjectTitleUpdateRequest(BaseModel):
    title: str


class PromptsUpdateRequest(BaseModel):
    probing_agent: PromptTemplates


class CreateProjectRequest(BaseModel):
    title: str
    folder_id: UUID4
    default_language: LanguageCode


class LoginData(BaseModel):
    email: str
    password: str
