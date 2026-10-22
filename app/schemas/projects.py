"""Projects, folders, collaborators and experiments."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import UUID4, EmailStr, Field, field_validator

from ainterviewer.config import InterviewConfig
from ainterviewer.types import LanguageCode, LanguageDict

from ..types import CollaboratorRole, ExternalParam, ProjectStatus
from .common import Schema
from .testing import TestSetupPublic
from .users import UserPublic


class CollaboratorBase(Schema):
    role: CollaboratorRole


class CollaboratorCreate(CollaboratorBase):
    email: EmailStr


class CollaboratorPublic(CollaboratorBase):
    id: UUID4
    user: UserPublic
    added_at: datetime


class FolderBase(Schema):
    title: str


class FolderCreate(FolderBase):
    collaborators: list[CollaboratorCreate] = Field(
        default=[], validation_alias="folder_collaborations"
    )


class FolderPublic(FolderBase):
    id: UUID4
    archived_at: datetime | None = None
    collaborators: list[CollaboratorPublic] = Field(
        default=[], validation_alias="folder_collaborations"
    )


class FolderUpdate(FolderBase): ...


class FolderWithProjectsPublic(FolderPublic):
    projects: list[ProjectPublic]


class ProjectBase(Schema):
    id: UUID4
    title: str
    created_at: datetime
    last_updated: datetime | None = None
    status: ProjectStatus = ProjectStatus.ACTIVE
    archived_at: datetime | None = None
    config: InterviewConfig
    external_params: list[ExternalParam] | None = None
    owner_id: UUID4


class ProjectLanguage(LanguageDict):
    """A language a project offers.

    Same shape as the library's `LanguageDict` plus whether it is the
    project's default and whether it is still in progress. `LanguageDict` itself stays free of the flag: it comes
    straight out of the shared `LANGUAGES` constant and knows nothing about
    projects.
    """

    is_default: bool
    #: Still being translated: in the draft but not offered to respondents.
    in_progress: bool


class ProjectPublic(ProjectBase):
    n_interviews: int | None = None
    available_languages: list[ProjectLanguage] | None = None
    tests: list[TestSetupPublic] | None = None
    owner: UserPublic


class ProjectPermissionsPublic(Schema):
    """What the caller may do in one project.

    The UI asks for this so it can leave out the actions the API would refuse,
    such as editing somebody else's comment. It is a convenience, never the
    check itself: every endpoint still enforces its own rights.
    """

    role: CollaboratorRole | None = None
    is_owner: bool
    can_moderate: bool


class ExperimentProjectCreate(Schema):
    """Input model for adding a project to an experiment."""

    project_id: UUID4
    weight: float | None = None


class ExperimentProjectPublic(Schema):
    """Public model for experiment-project association."""

    id: UUID4
    project_id: UUID4
    weight: float | None = None
    added_at: datetime


class ExperimentCreate(Schema):
    title: str
    projects: list[ExperimentProjectCreate]


class ExperimentPublic(Schema):
    id: UUID4
    title: str
    user_id: UUID4
    created_at: datetime
    status: ProjectStatus = ProjectStatus.ACTIVE
    projects: list[ExperimentProjectPublic] = []


class ProjectStatusChangeRequest(Schema):
    status: ProjectStatus


class ProjectTitleUpdateRequest(Schema):
    title: str


class ArchiveRequest(Schema):
    archived: bool


class MoveProjectRequest(Schema):
    folder_id: UUID4


class ProjectCreate(Schema):
    title: str
    default_language: LanguageCode


class ValidateExternalParamsRequest(Schema):
    external_params: dict[str, Any] | None = Field(
        None,
        description="Query parameters carried by the interview link.",
    )


class ExternalParamsReplace(Schema):
    params: list[ExternalParam]

    @field_validator("params")
    @classmethod
    def validate_params(cls, values: list[ExternalParam]):
        if len(values) != len({x.name for x in values}):
            raise ValueError("External parameter names must be unique")
        return values
