"""Projects, folders, collaborators and experiments."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import UUID4, Field, field_validator

from ainterviewer.config import InterviewConfig
from ainterviewer.types import LanguageCode, LanguageDict

from ..types import (
    CollaboratorRole,
    CompletionRedirects,
    ExperimentAllocation,
    ExternalParam,
    ProjectStatus,
)
from .common import NormalizedEmail, Schema
from .users import UserPublic


class CollaboratorBase(Schema):
    role: CollaboratorRole


class CollaboratorPublic(CollaboratorBase):
    id: UUID4
    user: UserPublic
    added_at: datetime


class CollaboratorInviteCreate(CollaboratorBase):
    email: NormalizedEmail


class CollaboratorInvitePublic(CollaboratorBase):
    """A pending invite as the folder's editors see it. It says nothing about
    whether the email has an account."""

    id: UUID4
    email: str
    invited_by: UserPublic | None
    created_at: datetime
    expires_at: datetime


class CollaboratorInviteRequest(Schema):
    """The token from an invite email's link."""

    token: str


class IssuedCollaboratorInvite(Schema):
    """A just-created or refreshed invite, with what its email needs: the raw
    token, which is stored only hashed, and whether the address has an
    account. Never on the wire -- `has_account` is exactly what the API keeps
    from the inviter."""

    invite: CollaboratorInvitePublic
    token: str
    has_account: bool
    recipient_first_name: str | None
    folder_title: str


class CollaboratorInviteDetails(Schema):
    """What an invite offers, shown to its recipient before they accept."""

    folder_id: UUID4
    folder_title: str
    role: CollaboratorRole
    invited_by_name: str | None
    expires_at: datetime


class FolderBase(Schema):
    title: str


class FolderCreate(FolderBase):
    collaborators: list[CollaboratorInviteCreate] = Field(
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
    #: Only respondents an experiment assigned here may take part.
    experiment_only: bool = False
    config: InterviewConfig
    external_params: list[ExternalParam] | None = None
    redirects: CompletionRedirects | None = None
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
    #: Unset counts as 1; zero or less leaves the project out of the draw.
    weight: float | None = None
    #: Completed interviews wanted from the project; None for no limit.
    target: int | None = Field(default=None, ge=1)
    #: Sets the project's own `experiment_only`; None leaves it as it is. It
    #: is the project's setting, so it holds for every experiment and link.
    experiment_only: bool | None = None


class ExperimentProjectPublic(Schema):
    """Public model for experiment-project association."""

    id: UUID4
    project_id: UUID4
    weight: float | None = None
    target: int | None = None
    added_at: datetime
    #: Respondents the experiment has given this project.
    assigned: int = 0
    #: Completed interviews from those respondents.
    completed: int = 0


class ExperimentCreate(Schema):
    title: str
    allocation: ExperimentAllocation = ExperimentAllocation.RANDOM
    projects: list[ExperimentProjectCreate]


class ExperimentUpdate(ExperimentCreate):
    """The experiment's settings in full: projects left out are removed."""


class ExperimentPublic(Schema):
    id: UUID4
    title: str
    user_id: UUID4
    created_at: datetime
    status: ProjectStatus = ProjectStatus.ACTIVE
    allocation: ExperimentAllocation = ExperimentAllocation.RANDOM
    projects: list[ExperimentProjectPublic] = []


class ExperimentEntryRequest(Schema):
    """A respondent opening an experiment's link. `pid` is the link's, when it
    names a known participant: their assignment is then kept for good."""

    pid: str | None = Field(default=None, max_length=200)


class ExperimentEntryResponse(Schema):
    """The project the experiment gave this respondent, to send them on to."""

    project_id: UUID4


class ExperimentOnlyRequest(Schema):
    experiment_only: bool


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


class ProjectCreateResponse(Schema):
    project_id: UUID4


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
