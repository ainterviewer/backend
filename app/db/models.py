from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import (
    UUID4,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    computed_field,
    field_validator,
)

from ainterviewer.agents.config import AgentConfigs
from ainterviewer.agents.prompts.models import Prompts
from ainterviewer.config import InterviewConfig
from ainterviewer.interview_guides import Image, InterviewGuide, SurveyItem
from ainterviewer.interview_guides.extra import Consent, Welcome
from ainterviewer.settings import settings as lib_settings
from ainterviewer.synthesize.interviewees import BackgroundInfoOptions, InterviewSubject
from ainterviewer.types import (
    Feedback,
    Interviewer,
    InterviewStatus,
    LanguageCode,
    LanguageDict,
    MessageRole,
    MessageType,
    TestType,
    TimeDelta,
)
from ainterviewer.utils import now

from ..settings import app_settings
from ..types import CollaboratorRole, ExternalParam, ProjectStatus, Scope, TestRunStatus
from ._extra import CustomEmailStr
from .types import AccessRequestStatus, AnnotationType, InterviewType


# TODO: Implement across more endpoints
class _Unset(Enum):
    UNSET = "UNSET"


UNSET = _Unset.UNSET


class _BaseModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class AccessRequestBase(_BaseModel):
    name: str
    email: CustomEmailStr
    organization: str | None = None
    message: str | None = None


class AccessRequestCreate(AccessRequestBase): ...


class AccessRequestPublic(AccessRequestBase):
    id: UUID4
    created_at: datetime
    updated_at: datetime
    status: AccessRequestStatus
    processed_by_id: UUID4 | None


class InvitationBase(_BaseModel):
    expires_at: datetime | None = None
    reuseable: bool = False
    user_scope: Scope = Scope.USER
    user_expires: datetime | TimeDelta | None = None
    title: str | None = None
    access_request_id: UUID4 | None = None


class InvitationCreate(InvitationBase): ...


class InvitationUpdate(BaseModel):
    email: str | None | _Unset = UNSET
    expires_at: datetime | None | _Unset = UNSET
    reuseable: bool | _Unset = UNSET
    user_scope: Scope | _Unset = UNSET
    user_expires: datetime | TimeDelta | None | _Unset = UNSET
    title: str | None | _Unset = UNSET


class InvitationPublic(InvitationBase):
    id: UUID4
    email: str | None

    @computed_field()
    def invitation_link(self) -> str:
        return f"{app_settings.sveltekit_platform_public_addr}/sign-up?token={self.id}"


class UserBase(_BaseModel):
    email: EmailStr
    first_name: str
    last_name: str | None = None
    created_at: datetime
    last_active: datetime
    last_login: datetime
    scope: Scope = Scope.USER


class UserCreateRequest(UserBase):
    """API request model for user registration."""

    invite_token: UUID4 | str | None = None

    created_at: datetime = Field(default_factory=now)
    last_active: datetime = Field(default_factory=now)
    last_login: datetime = Field(default_factory=now)
    research_consent: bool = False
    password: str = Field(min_length=8)

    @field_validator("password")
    @classmethod
    def _password_within_bcrypt_limit(cls, value: str) -> str:
        # bcrypt silently truncates input beyond 72 bytes (not chars), so
        # reject anything longer to avoid surprising password equivalence.
        if len(value.encode("utf-8")) > 72:
            raise ValueError("Password must be at most 72 bytes")
        return value


class UserCreate(UserCreateRequest):
    """Internal model for creating a user, includes snapshot fields."""

    registration_token: str | None = None
    invitation_title: str | None = None
    expires_at: datetime | None = None
    access_request_message: str | None = None
    organization: str | None = None


class UserPrivate(UserBase):
    id: UUID4
    password: str
    with_demo_features: bool
    organization: str | None = None
    email_verified: bool = False
    two_factor_enabled: bool = True


class UserPublic(UserBase):
    id: UUID4
    invitation_title: str | None = None
    expires_at: datetime | None = None
    with_demo_features: bool
    organization: str | None = None


class UserAdmin(UserPublic):
    access_request_message: str | None = None
    admin_note: str | None = None
    admin_note_updated_at: datetime | None = None
    two_factor_enabled: bool = True


class UserAdminUpdate(BaseModel):
    scope: Scope | _Unset = UNSET
    with_demo_features: bool | _Unset = UNSET
    organization: str | None | _Unset = UNSET
    expires_at: datetime | None | _Unset = UNSET
    two_factor_enabled: bool | _Unset = UNSET


class UserSelfUpdate(BaseModel):
    """Profile fields a user may change on their own account."""

    first_name: str | _Unset = UNSET
    last_name: str | None | _Unset = UNSET
    organization: str | None | _Unset = UNSET


class Collaborator(_BaseModel):
    email: EmailStr
    role: CollaboratorRole


class CollaboratorBase(_BaseModel):
    role: CollaboratorRole


class CollaboratorCreate(CollaboratorBase):
    email: EmailStr


class CollaboratorPublic(CollaboratorBase):
    id: UUID4
    user: UserPublic
    added_at: datetime


class ProjectLocalizationBase(_BaseModel):
    project_id: UUID4
    language: LanguageCode
    consent: Consent | None
    welcome: Welcome | None
    interview_guide: InterviewGuide
    prompts: Prompts
    agent_configs: AgentConfigs
    created_at: datetime
    last_updated: Optional[datetime] = None


class ProjectLocalizationCreate(_BaseModel):
    language: LanguageCode
    interview_guide: Optional[InterviewGuide] = None
    prompts: Optional[Prompts] = None
    agent_configs: Optional[AgentConfigs] = None


class ProjectLocalizationPublic(ProjectLocalizationBase):
    id: UUID4


class ProjectFolderBase(_BaseModel):
    title: str


class ProjectFolderCreate(ProjectFolderBase):
    collaborators: list[Collaborator] = Field(
        default=[], validation_alias="folder_collaborations"
    )


class ProjectFolderPublic(ProjectFolderBase):
    id: UUID4
    collaborators: list[CollaboratorPublic] = Field(
        default=[], validation_alias="folder_collaborations"
    )


class ProjectFolderEdit(ProjectFolderBase): ...


class ProjectFolderWithProjects(ProjectFolderPublic):
    projects: list[ProjectPublic]


class ProjectBase(_BaseModel):
    model_config = {"extra": "forbid", "use_enum_values": True}

    id: UUID4
    title: str
    created_at: datetime
    last_updated: Optional[datetime] = None
    status: ProjectStatus = ProjectStatus.ACTIVE
    config: InterviewConfig
    external_params: list[ExternalParam] | None = None
    owner_id: UUID4


class ProjectCreate(_BaseModel):
    title: str
    config: Optional[InterviewConfig] = None


class ProjectPublic(ProjectBase):
    n_interviews: int | None = None
    available_languages: list[LanguageDict] | None = None
    tests: list[TestSetupPublic] | None = None
    owner: UserPublic


class ProjectPublicWithTests(ProjectPublic):
    tests: list[TestSetupPublic]


class ExperimentProjectCreate(_BaseModel):
    """Input model for adding a project to an experiment."""

    project_id: UUID4
    weight: Optional[float] = None


class ExperimentProjectPublic(_BaseModel):
    """Public model for experiment-project association."""

    id: UUID4
    project_id: UUID4
    weight: Optional[float] = None
    added_at: datetime


class ExperimentCreate(_BaseModel):
    title: str
    projects: list[ExperimentProjectCreate]


class ExperimentPublic(_BaseModel):
    id: UUID4
    title: str
    user_id: UUID4
    created_at: datetime
    status: ProjectStatus = ProjectStatus.ACTIVE
    projects: list[ExperimentProjectPublic] = []


class InterviewBase(_BaseModel):
    id: UUID4
    interview_guide: InterviewGuide | None
    language: LanguageCode = "EN"
    interviewer: Interviewer = Interviewer.AI
    status: InterviewStatus = InterviewStatus.INACTIVE
    type: InterviewType = InterviewType.DISTRIBUTED
    created_at: datetime
    last_updated: Optional[datetime] = None
    total_time_spent: int = 0
    survey_token: Optional[str] = None
    user_agent: Optional[str] = None
    ip_address: Optional[str] = None
    referer: Optional[str] = None
    platform_version: Optional[str] = None
    test_name: str | None = None


class InterviewCreate(_BaseModel):
    interview_guide: InterviewGuide
    language: LanguageCode = "EN"
    interviewer: Interviewer = Interviewer.AI
    project_id: UUID4
    experiment_id: Optional[UUID4] = None


class InterviewPublic(InterviewBase):
    n_messages: int
    messages: list["MessagePublic"]


class InterviewSummaryPublic(_BaseModel):
    id: UUID4
    language: LanguageCode = "EN"
    interviewer: Interviewer = Interviewer.AI
    status: InterviewStatus
    type: InterviewType
    created_at: datetime
    last_updated: Optional[datetime] = None
    total_time_spent: int = 0
    n_messages: int
    messages: list["MessagePublic"]
    test_name: str | None = None


class MessageBase(_BaseModel):
    message_id: int
    content: str
    role: MessageRole
    interview_id: UUID4
    project_id: UUID4
    message_type: MessageType = MessageType.TEXT
    section: Optional[int] = None
    main_question: Optional[int] = None
    sub_question: Optional[int] = None
    is_introduction: bool = False
    outro: bool = False
    timed: bool = False
    can_answer: bool = True
    include_in_history: bool = True
    attachment: Optional[Path] = None
    audio_file: Optional[str] = None
    feedback: Optional[Feedback] = None
    created_at: datetime
    image: Optional[Image | list[Image]] = None
    survey_item: Optional[SurveyItem] = None
    skipped_by_condition: bool = False


class MessageCreate(_BaseModel):
    message_id: int
    content: str
    role: MessageRole
    interview_id: UUID4
    project_id: UUID4
    message_type: MessageType = MessageType.TEXT
    section: Optional[int] = None
    main_question: Optional[int] = None
    sub_question: Optional[int] = None
    is_introduction: bool = False
    outro: bool = False
    timed: bool = False
    can_answer: bool = True
    include_in_history: bool = True
    attachment: Optional[Path] = None
    audio_file: Optional[str] = None
    feedback: Optional[Feedback] = None
    image: Optional[Image | list[Image]] = None
    survey_item: Optional[SurveyItem] = None
    skipped_by_condition: bool = False


class MessagePublic(MessageBase):
    id: UUID4
    annotations: list["MessageAnnotationPublic"] = []

    interview_type: InterviewType


class TaskBase(_BaseModel):
    id: UUID4
    created_at: datetime
    message_id: int
    interview_id: UUID4
    project_id: UUID4
    task: str
    reason: Optional[str] = None
    content: Optional[str] = None
    response: Optional[str] = None
    model: Optional[str] = None
    time_spend: Optional[int] = None


class TaskCreate(_BaseModel):
    message_id: int
    interview_id: UUID4
    project_id: UUID4
    task: str
    reason: Optional[str] = None
    content: Optional[str] = None
    response: Optional[str] = None
    model: Optional[str] = None
    time_spend: Optional[int] = None


class TaskPublic(TaskBase):
    pass


class TestSetupBase(_BaseModel):
    name: Optional[str] = None
    type: TestType
    project_id: UUID4
    answering_model: str = lib_settings.llm.default_model
    last_updated: Optional[datetime] = None
    language: LanguageCode = "EN"
    n_interviews: int = 5
    delay_before_answers: Optional[tuple[float, float]] = None


class TestSetupCreate(TestSetupBase):
    pass


class TestSetupPublic(TestSetupBase):
    n_runs: int
    id: UUID4
    created_at: datetime
    background_info: BackgroundInfoOptions | None = None
    fixed_answers: list[str] | None = None
    fixed_personas: list[str] | None = None


class TestRunBase(_BaseModel):
    test_setup_id: UUID4
    language: LanguageCode = "EN"
    n_interviews: int
    answering_model: str
    delay_before_answers: Optional[tuple[float, float]] = None


class TestRunCreate(TestRunBase):
    pass


class TestRunPublic(TestRunBase):
    id: UUID4
    created_at: datetime
    last_updated: Optional[datetime] = None
    status: TestRunStatus


class ParticipantBase(_BaseModel):
    name: str | None = None
    email: EmailStr | None = None
    pid: str | None = None
    participating: bool = True
    lang: LanguageCode | None = None


class ParticipantCreate(ParticipantBase):
    pass


class ParticipantUpdate(BaseModel):
    name: str | None | _Unset = UNSET
    email: EmailStr | None | _Unset = UNSET
    pid: str | _Unset = UNSET
    participating: bool | _Unset = UNSET


class ParticipantPublic(ParticipantBase):
    id: UUID4
    project_id: UUID4
    participant_id: UUID4
    folder_id: UUID4
    created_at: datetime
    pid: str
    latest_interview_at: datetime | None = None
    latest_interview_status: InterviewStatus | None = None


class IntervieweeBase(_BaseModel):
    interview_id: UUID4
    interview_subject: InterviewSubject | str


class IntervieweeCreate(IntervieweeBase):
    pass


class IntervieweePublic(IntervieweeBase):
    id: UUID4


############
# Analysis #
############


class AnalysisCategoryBase(_BaseModel):
    project_id: UUID4
    name: str
    description: Optional[str] = None
    type: AnnotationType
    color: str
    min_value: Optional[int] = None
    max_value: Optional[int] = None


class AnalysisCategoryCreate(AnalysisCategoryBase):
    pass


class AnalysisCategoryPublic(AnalysisCategoryBase):
    id: UUID4
    created_at: datetime


class FilteredMessagesRequest(_BaseModel):
    category_ids: list[UUID4] | None = None
    search_text: str | None = None
    exact_match: bool = False
    case_sensitive: bool = False
    questions: list[tuple[int, int]] | None = None
    include_previous_on_user: bool = True


class AnnotationValueBase(_BaseModel):
    category_id: UUID4
    value_int: int


class AnnotationValueCreate(AnnotationValueBase):
    pass


class AnnotationValuePublic(AnnotationValueBase):
    id: UUID4


class MessageAnnotationBase(_BaseModel):
    message_id: UUID4
    user_id: UUID4
    comment: str | None = None


class MessageAnnotationCreate(MessageAnnotationBase):
    values: list[AnnotationValueCreate]


class MessageAnnotationPublic(MessageAnnotationBase):
    id: UUID4
    created_at: datetime
    updated_at: datetime
    values: list[AnnotationValuePublic]
