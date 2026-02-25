from __future__ import annotations

from datetime import datetime
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

from ainterviewer.agents.prompts.models import Prompts
from ainterviewer.config import AgentConfigs, InterviewConfig
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

from ..auth import InviteToken
from ..settings import app_settings
from ..types import CollaboratorRole, ProjectStatus, Scope, TestRunStatus
from ._extra import CustomEmailStr
from .types import AccessRequestStatus, AnnotationType, InterviewType


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
    expires_at: datetime
    reuseable: bool = False
    user_scope: Scope = Scope.USER
    user_expires: datetime | None = None
    title: str | None = None


class InvitationCreate(InvitationBase): ...


class InvitationPublic(InvitationBase):
    id: UUID4
    expires_at: datetime

    @computed_field()
    def invitation_link(self) -> str:
        return f"{app_settings.app.app_endpoint}/sign-up?token={self.id}"


class UserBase(_BaseModel):
    email: EmailStr
    first_name: str
    last_name: str | None = None
    created_at: datetime
    last_active: datetime
    last_login: datetime
    scope: Scope = Scope.USER


class UserCreate(UserBase):
    invite_token: UUID4 | None = None

    created_at: datetime = Field(default_factory=now)
    last_active: datetime = Field(default_factory=now)
    last_login: datetime = Field(default_factory=now)
    research_consent: bool = False
    password: str


class UserPublic(UserBase):
    id: UUID4
    invite_token: str | None = Field()

    @field_validator("invite_token", mode="before")
    def transform_invite_token(cls, v: str | None) -> str | None:
        if v is not None:
            if len(v.split(":::")) == 2:
                return v.split(":::")[0]

        return None


class UserPrivate(UserBase):
    id: UUID4
    password: str
    invite_token: str | None = Field()


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
    comment: Optional[str] = None


class MessageAnnotationCreate(MessageAnnotationBase):
    values: list[AnnotationValueCreate]


class MessageAnnotationPublic(MessageAnnotationBase):
    id: UUID4
    created_at: datetime
    updated_at: datetime
    values: list[AnnotationValuePublic]
