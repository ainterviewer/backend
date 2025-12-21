import datetime
import uuid
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import JSON, ForeignKey, MetaData, Text, UniqueConstraint, Uuid, select
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.ext.associationproxy import association_proxy
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship
from sqlalchemy.sql import func

from ainterviewer.config import AgentConfigs, InterviewConfig
from ainterviewer.interview_guides import Image, InterviewGuide, SurveyItem
from ainterviewer.interview_guides.extra import Consent, Welcome
from ainterviewer.prompts.models import DEFAULT_PROMPTS, Prompts
from ainterviewer.synthesize.interviewees import (
    DEFAULT_BACKGROUND_INFO_OPTIONS,
    BackgroundInfoOptions,
    InterviewSubject,
)
from ainterviewer.types import (
    Feedback,
    Interviewer,
    LanguageCode,
    MessageRole,
    MessageType,
    TestType,
)
from ainterviewer.utils import now

from ..types import ProjectStatus, Scope, TestRunStatus
from ._extra import PydanticJSONB
from .types import AccessRequestStatus, AnnotationType, LanguageType, ProjectRole

naming_convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata_obj = MetaData(naming_convention=naming_convention)


class Base(DeclarativeBase):
    metadata = metadata_obj


##########
# Access #
##########


class AccessRequestTable(Base):
    __tablename__ = "access_requests"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4, unique=True
    )
    name: Mapped[str] = mapped_column()
    email: Mapped[str] = mapped_column(unique=True)
    organization: Mapped[str | None] = mapped_column()
    message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime.datetime] = mapped_column(default=now, onupdate=now)
    status: Mapped[AccessRequestStatus] = mapped_column(
        default=AccessRequestStatus.WAITING
    )
    processed_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("user.id"))

    # Relationships
    processed_by: Mapped[Optional["UserTable"]] = relationship(
        back_populates="processed_requests"
    )


class InvitationTable(Base):
    __tablename__ = "invitation"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4, unique=True
    )
    email: Mapped[str] = mapped_column(unique=True)
    created_at: Mapped[datetime.datetime] = mapped_column(default=now)
    expires_at: Mapped[datetime.datetime] = mapped_column()
    redeemed_at: Mapped[datetime.datetime | None] = mapped_column(default=None)

    # Relationships


########
# User #
########


class UserTable(Base):
    __tablename__ = "user"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4, unique=True
    )
    email: Mapped[str] = mapped_column(unique=True)
    password: Mapped[str] = mapped_column()
    name: Mapped[str] = mapped_column()
    created_at: Mapped[datetime.datetime] = mapped_column(default=now)
    last_active: Mapped[datetime.datetime] = mapped_column(default=now)
    last_login: Mapped[datetime.datetime] = mapped_column(default=now)
    scope: Mapped[Scope] = mapped_column(SQLEnum(Scope), default=Scope.USER)
    invite_token: Mapped[str | None] = mapped_column()
    research_consent: Mapped[bool] = mapped_column(default=False)

    # Relationships
    processed_requests: Mapped[list["AccessRequestTable"]] = relationship(
        back_populates="processed_by"
    )
    folder_collaborations: Mapped[list["CollaboratorTable"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        foreign_keys="CollaboratorTable.user_id",
    )
    collaborating_folders = association_proxy(
        "folder_collaborations",
        "folder",
        creator=lambda folder: CollaboratorTable(folder=folder),
    )
    annotations: Mapped[list["MessageAnnotationTable"]] = relationship(
        back_populates="user"
    )


#############
# Project #
#############


class ProjectFolderTable(Base):
    __tablename__ = "projectfolder"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4, unique=True
    )
    title: Mapped[str] = mapped_column()
    created_at: Mapped[datetime.datetime] = mapped_column(default=now)
    last_updated: Mapped[datetime.datetime | None] = mapped_column(default=now)

    # Relationsiphs
    projects: Mapped[list["ProjectTable"]] = relationship(back_populates="folder")
    folder_collaborations: Mapped[list["CollaboratorTable"]] = relationship(
        back_populates="folder",
        cascade="all, delete-orphan",
        foreign_keys="CollaboratorTable.folder_id",
    )
    collaborators = association_proxy(
        "folder_collaborations",
        "user",
        creator=lambda user: CollaboratorTable(user=user),
    )


class ProjectTable(Base):
    __tablename__ = "project"
    __table_args__ = (UniqueConstraint("title", "folder_id", name="title"),)

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4, unique=True
    )
    folder_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projectfolder.id"))
    title: Mapped[str] = mapped_column()
    created_at: Mapped[datetime.datetime] = mapped_column(default=now)
    last_updated: Mapped[datetime.datetime | None] = mapped_column(default=now)
    status: Mapped[ProjectStatus] = mapped_column(
        SQLEnum(ProjectStatus), default=ProjectStatus.ACTIVE
    )
    config: Mapped[InterviewConfig] = mapped_column(
        PydanticJSONB(InterviewConfig),
        default=InterviewConfig,
    )

    # Relationships
    interviews: Mapped[list["InterviewTable"]] = relationship(back_populates="project")
    localizations: Mapped[list["ProjectLocalizationTable"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    folder: Mapped["ProjectFolderTable"] = relationship(back_populates="projects")
    tests: Mapped[list["TestSetupTable"]] = relationship(back_populates="project")
    analysis_categories: Mapped[list["AnalysisCategoryTable"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )

    @property
    def collaborators(self) -> list["UserTable"]:
        return self.folder.collaborators if self.folder else []


class ProjectLocalizationTable(Base):
    __tablename__ = "projectlocalization"
    __table_args__ = (
        UniqueConstraint("project_id", "language", name="unique_project_language"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4, unique=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("project.id"))
    language: Mapped[LanguageCode] = mapped_column(LanguageType, index=True)
    consent: Mapped[Consent | None] = mapped_column(PydanticJSONB(Consent))
    welcome: Mapped[Welcome | None] = mapped_column(PydanticJSONB(Welcome))
    interview_guide: Mapped[InterviewGuide] = mapped_column(
        PydanticJSONB(InterviewGuide),
        default=lambda: InterviewGuide(),
    )
    prompts: Mapped[Prompts] = mapped_column(
        PydanticJSONB(Prompts), default=DEFAULT_PROMPTS
    )
    agent_configs: Mapped[AgentConfigs] = mapped_column(
        PydanticJSONB(AgentConfigs),
        default=lambda: AgentConfigs(),
    )
    created_at: Mapped[datetime.datetime] = mapped_column(default=now)
    last_updated: Mapped[datetime.datetime | None] = mapped_column()

    # Relationships
    project: Mapped["ProjectTable"] = relationship(back_populates="localizations")


#################
# Collaborators #
#################


class CollaboratorTable(Base):
    __tablename__ = "collaborator"
    __table_args__ = (UniqueConstraint("folder_id", "user_id", name="uq_collaborator"),)

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4, unique=True
    )
    folder_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projectfolder.id"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user.id"))
    added_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("user.id"))
    added_at: Mapped[datetime.datetime] = mapped_column(default=now)
    project_role: Mapped[ProjectRole] = mapped_column(SQLEnum(ProjectRole))

    # Relationships
    folder: Mapped["ProjectFolderTable"] = relationship(
        back_populates="folder_collaborations"
    )
    user: Mapped["UserTable"] = relationship(
        back_populates="folder_collaborations",
        foreign_keys=[user_id],
    )
    added_by: Mapped[Optional["UserTable"]] = relationship(foreign_keys=[added_by_id])


############
# Redirect #
############


class ExperimentTable(Base):
    __tablename__ = "experiment"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4, unique=True
    )
    title: Mapped[str] = mapped_column()
    created_at: Mapped[datetime.datetime] = mapped_column(default=now)
    project_ids: Mapped[Any] = mapped_column(JSON)
    weights: Mapped[Any | None] = mapped_column(JSON)
    status: Mapped[ProjectStatus] = mapped_column(
        SQLEnum(ProjectStatus), default=ProjectStatus.ACTIVE
    )

    # Relationships
    interviews: Mapped[list["InterviewTable"]] = relationship(
        back_populates="experiment"
    )


#############
# Interview #
#############


class InterviewTable(Base):
    __tablename__ = "interview"
    __table_args__ = (
        UniqueConstraint("id", "project_id", name="_unique_interview_ids"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4, unique=True
    )
    interview_guide: Mapped[InterviewGuide] = mapped_column(
        PydanticJSONB(InterviewGuide)
    )
    language: Mapped[LanguageCode] = mapped_column(LanguageType, default="EN")
    interviewer: Mapped[Interviewer] = mapped_column(
        SQLEnum(Interviewer), default=Interviewer.AI
    )
    is_complete: Mapped[bool] = mapped_column(default=False)
    is_active: Mapped[bool] = mapped_column(default=False)
    is_synthetic: Mapped[bool] = mapped_column(default=False)
    is_test: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime.datetime] = mapped_column(default=now)
    last_updated: Mapped[datetime.datetime | None] = mapped_column()
    total_time_spent: Mapped[int] = mapped_column(default=0)
    survey_token: Mapped[str | None] = mapped_column()
    user_agent: Mapped[str | None] = mapped_column()
    ip_address: Mapped[str | None] = mapped_column()
    referer: Mapped[str | None] = mapped_column()
    external_params: Mapped[str | None] = mapped_column()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("project.id"))
    experiment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("experiment.id"))

    # Relationships
    project: Mapped["ProjectTable"] = relationship(back_populates="interviews")
    experiment: Mapped[Optional["ExperimentTable"]] = relationship(
        back_populates="interviews"
    )
    messages: Mapped[list["MessageTable"]] = relationship(back_populates="interview")

    @hybrid_property
    def n_messages(self) -> int:
        return len(self.messages) if self.messages else 0

    @n_messages.expression
    def n_messages(cls):
        return (
            select(func.count(MessageTable.id))
            .where(MessageTable.interview_id == cls.id)
            .scalar_subquery()
        )


class InterviewService:
    """Service class to handle Interview business logic"""

    def __init__(self, db: Session):
        self.db = db

    def get_n_messages(self, interview: InterviewTable) -> int:
        """Get the number of messages for an interview"""
        return len(interview.messages)


###########
# Message #
###########


class MessageTable(Base):
    __tablename__ = "message"
    __table_args__ = (
        UniqueConstraint(
            "message_id", "interview_id", "project_id", name="_unique_message_ids"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4, unique=True
    )
    message_id: Mapped[int] = mapped_column()
    content: Mapped[str] = mapped_column(Text)
    role: Mapped[MessageRole] = mapped_column(SQLEnum(MessageRole))
    interview_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("interview.id"))
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("project.id"))
    message_type: Mapped[MessageType] = mapped_column(
        SQLEnum(MessageType), default=MessageType.TEXT
    )
    section: Mapped[int | None] = mapped_column()
    main_question: Mapped[int | None] = mapped_column()
    sub_question: Mapped[int | None] = mapped_column()
    is_introduction: Mapped[bool] = mapped_column(default=False)
    outro: Mapped[bool] = mapped_column(default=False)
    timed: Mapped[bool] = mapped_column(default=False)
    can_answer: Mapped[bool] = mapped_column(default=True)
    include_in_history: Mapped[bool] = mapped_column(default=True)
    attachment: Mapped[str | None] = mapped_column()  # Path stored as string
    feedback: Mapped[Feedback | None] = mapped_column(SQLEnum(Feedback))
    created_at: Mapped[datetime.datetime] = mapped_column(default=now)
    image: Mapped[Image | None] = mapped_column(PydanticJSONB(Image))
    survey_item: Mapped[SurveyItem | None] = mapped_column(PydanticJSONB(SurveyItem))

    # Relationships
    interview: Mapped["InterviewTable"] = relationship(back_populates="messages")
    annotations: Mapped[list["MessageAnnotationTable"]] = relationship(
        back_populates="message", cascade="all, delete-orphan"
    )

    @hybrid_property
    def is_synthetic(self) -> bool:
        return self.interview.is_synthetic if self.interview else False

    @is_synthetic.expression
    def is_synthetic(cls):
        return (
            select(InterviewTable.is_synthetic)
            .where(InterviewTable.id == cls.interview_id)
            .scalar_subquery()
        )

    @hybrid_property
    def is_test(self) -> bool:
        return self.interview.is_test if self.interview else False

    @is_test.expression
    def is_test(cls):
        return (
            select(InterviewTable.is_test)
            .where(InterviewTable.id == cls.interview_id)
            .scalar_subquery()
        )


########
# Task #
########


class TaskTable(Base):
    __tablename__ = "task"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4, unique=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(default=now)
    message_id: Mapped[int] = mapped_column()
    interview_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("interview.id"))
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("project.id"))
    task: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    context: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)
    response: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column()
    time_spend: Mapped[int | None] = mapped_column()


###################
# Synthetic Tests #
###################


class TestSetupTable(Base):
    __tablename__ = "testsetup"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4, unique=True
    )
    name: Mapped[str | None] = mapped_column()
    type: Mapped[TestType] = mapped_column(SQLEnum(TestType))
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("project.id"))
    created_at: Mapped[datetime.datetime] = mapped_column(default=now)
    last_updated: Mapped[datetime.datetime | None] = mapped_column()
    language: Mapped[LanguageCode] = mapped_column(LanguageType, default="EN")
    n_interviews: Mapped[int] = mapped_column(default=5)
    answering_model: Mapped[str | None] = mapped_column()
    delay_before_answers: Mapped[Any | None] = mapped_column(JSON)
    background_info: Mapped[BackgroundInfoOptions] = mapped_column(
        PydanticJSONB(BackgroundInfoOptions),
        default=DEFAULT_BACKGROUND_INFO_OPTIONS,
    )
    fixed_answers: Mapped[list[str] | None] = mapped_column(JSON)
    fixed_personas: Mapped[Any | None] = mapped_column(JSON)

    # Relationships
    test_runs: Mapped[list["TestRunTable"]] = relationship(back_populates="test_setup")
    project: Mapped["ProjectTable"] = relationship(back_populates="tests")

    @hybrid_property
    def n_runs(self) -> int:
        """Get the number of test runs for a test setup"""
        return len(self.test_runs)


class TestRunTable(Base):
    __tablename__ = "testrun"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4, unique=True
    )
    test_setup_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("testsetup.id"))
    language: Mapped[LanguageCode] = mapped_column(LanguageType, default="EN")
    n_interviews: Mapped[int] = mapped_column()
    answering_model: Mapped[str | None] = mapped_column()
    delay_before_answers: Mapped[Any | None] = mapped_column(JSON)
    created_at: Mapped[datetime.datetime] = mapped_column(default=now)
    last_updated: Mapped[datetime.datetime | None] = mapped_column()
    status: Mapped[TestRunStatus] = mapped_column(
        SQLEnum(TestRunStatus), default=TestRunStatus.PENDING
    )

    # Relationships
    test_setup: Mapped["TestSetupTable"] = relationship(back_populates="test_runs")


class IntervieweeTable(Base):
    __tablename__ = "interviewee"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4, unique=True
    )
    interview_id: Mapped[uuid.UUID] = mapped_column()
    project_id: Mapped[uuid.UUID] = mapped_column()
    interview_subject: Mapped[InterviewSubject] = mapped_column(
        PydanticJSONB(InterviewSubject)
    )


############
# Analysis #
############


class AnalysisCategoryTable(Base):
    __tablename__ = "analysis_category"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="unique_project_category_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4, unique=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("project.id"))
    name: Mapped[str] = mapped_column()
    description: Mapped[str | None] = mapped_column(Text)
    type: Mapped[AnnotationType] = mapped_column(SQLEnum(AnnotationType))
    color: Mapped[str] = mapped_column()
    min_value: Mapped[int | None] = mapped_column()
    max_value: Mapped[int | None] = mapped_column()
    created_at: Mapped[datetime.datetime] = mapped_column(default=now)

    # Relationships
    project: Mapped["ProjectTable"] = relationship(back_populates="analysis_categories")
    values: Mapped[list["AnnotationValueTable"]] = relationship(
        back_populates="category", cascade="all, delete-orphan"
    )


class MessageAnnotationTable(Base):
    __tablename__ = "message_annotation"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4, unique=True
    )
    message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("message.id"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user.id"))
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime.datetime] = mapped_column(default=now, onupdate=now)

    # Relationships
    message: Mapped["MessageTable"] = relationship(back_populates="annotations")
    user: Mapped["UserTable"] = relationship(back_populates="annotations")
    values: Mapped[list["AnnotationValueTable"]] = relationship(
        back_populates="annotation", cascade="all, delete-orphan"
    )


class AnnotationValueTable(Base):
    __tablename__ = "annotation_value"
    __table_args__ = (
        UniqueConstraint(
            "annotation_id", "category_id", name="unique_annotation_category"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4, unique=True
    )
    annotation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("message_annotation.id")
    )
    category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("analysis_category.id"))
    value_int: Mapped[int] = mapped_column()

    # Relationships
    annotation: Mapped["MessageAnnotationTable"] = relationship(back_populates="values")
    category: Mapped["AnalysisCategoryTable"] = relationship(back_populates="values")
