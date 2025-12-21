from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import List, Literal, Optional

from pydantic import (
    UUID4,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    computed_field,
    model_validator,
)

from ainterviewer.config import AgentConfigs, InterviewConfig
from ainterviewer.interview_guides import Image, InterviewGuide, SurveyItem
from ainterviewer.interview_guides.extra import Consent, Welcome
from ainterviewer.prompts.models import Prompts
from ainterviewer.synthesize.interviewees import BackgroundInfoOptions, InterviewSubject
from ainterviewer.types import (
    Feedback,
    Interviewer,
    LanguageCode,
    LanguageDict,
    MessageRole,
    MessageType,
    TestType,
)
from ainterviewer.utils import now

from ..settings import app_settings
from ..types import ProjectStatus, Scope, TestRunStatus
from ._extra import CustomEmailStr
from .types import AccessRequestStatus, AnnotationType


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
    token: str
    created_at: datetime
    expires_at: datetime
    used: bool = False


class InvitationCreate(_BaseModel):
    expires_at: datetime


class InvitationPublic(_BaseModel):
    id: UUID4
    expires_at: datetime

    @computed_field()
    def invitation_link(self) -> str:
        return f"{app_settings.app.endpoint}/login?token={self.id}#signup"


class UserBase(_BaseModel):
    email: EmailStr
    name: str
    created_at: datetime
    last_active: datetime
    last_login: datetime
    scope: Scope = Scope.USER


class UserCreate(UserBase):
    created_at: datetime = Field(default_factory=now)
    last_active: datetime = Field(default_factory=now)
    last_login: datetime = Field(default_factory=now)
    invite_token: Optional[UUID4 | Literal["test"]] = None
    research_consent: bool = False
    password: str


class UserPublic(UserBase):
    id: UUID4


class UserPrivate(UserBase):
    id: UUID4
    password: str


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
    pass


class ProjectFolderPublic(ProjectFolderBase):
    id: UUID4


class ProjectFolderEdit(ProjectFolderBase):
    id: UUID4
    title: str


class ProjectFolderDelete(_BaseModel):
    id: UUID4


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


class ProjectCreate(_BaseModel):
    title: str
    config: Optional[InterviewConfig] = None


class ProjectPublic(ProjectBase):
    n_interviews: int | None = None
    available_languages: List[LanguageDict] | None = None
    tests: list[TestSetupPublic] | None = None


class ProjectPublicWithTests(ProjectPublic):
    tests: list[TestSetupPublic]  # type: ignore


class ExperimentBase(_BaseModel):
    id: UUID4
    title: str
    created_at: datetime
    project_ids: List[str]
    weights: Optional[List[float]] = None
    status: ProjectStatus = ProjectStatus.ACTIVE

    @model_validator(mode="after")
    def validate_model(self):
        n_interviews = self.project_ids
        if weights := self.weights:
            if len(weights) != len(n_interviews):
                raise ValueError(
                    "Redirect probabilities must match the number of interview IDs."
                )
        else:
            self.weights = [1.0] * len(n_interviews)
        return self


class ExperimentCreate(_BaseModel):
    title: str
    project_ids: List[str]
    weights: Optional[List[float]] = None


class ExperimentPublic(ExperimentBase):
    pass


class InterviewBase(_BaseModel):
    id: UUID4
    interview_guide: InterviewGuide | None
    language: LanguageCode = "EN"
    interviewer: Interviewer = Interviewer.AI
    is_complete: bool = False
    is_active: bool = False
    is_synthetic: bool = False
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
    is_complete: bool = False
    is_active: bool = False
    is_synthetic: bool = False
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
    image: Optional[Image | List[Image]] = None
    survey_item: Optional[SurveyItem] = None


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
    image: Optional[Image | List[Image]] = None
    survey_item: Optional[SurveyItem] = None


class MessagePublic(MessageBase):
    id: UUID4
    annotations: list["MessageAnnotationPublic"] = []

    is_test: bool
    is_synthetic: bool


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
    last_updated: Optional[datetime] = None
    language: LanguageCode = "EN"
    n_interviews: int = 5
    answering_model: Optional[str] = None
    delay_before_answers: Optional[tuple[float, float]] = None


class TestSetupCreate(TestSetupBase):
    pass


class TestSetupPublic(TestSetupBase):
    n_runs: int
    id: UUID4
    created_at: datetime
    background_info: Optional[BackgroundInfoOptions] = None
    fixed_answers: Optional[List[str]] = None


class TestRunBase(_BaseModel):
    test_setup_id: UUID4
    language: LanguageCode = "EN"
    n_interviews: int
    answering_model: Optional[str] = None
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
    project_id: UUID4
    interview_subject: InterviewSubject


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
    values: List[AnnotationValueCreate]


class MessageAnnotationPublic(MessageAnnotationBase):
    id: UUID4
    created_at: datetime
    updated_at: datetime
    values: List[AnnotationValuePublic]
