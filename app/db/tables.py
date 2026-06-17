import datetime
import uuid
from typing import Any, Optional
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy import JSON, ForeignKey, MetaData, Text, UniqueConstraint, Uuid, select
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.ext.associationproxy import association_proxy
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship
from sqlalchemy.sql import func

from ainterviewer.agents.config import AgentConfigs
from ainterviewer.agents.prompts.models import DEFAULT_PROMPTS, Prompts
from ainterviewer.config import InterviewConfig
from ainterviewer.interview_guides import Image, InterviewGuide, SurveyItem
from ainterviewer.interview_guides.extra import Consent, Welcome
from ainterviewer.synthesize.interviewees import (
    DEFAULT_BACKGROUND_INFO_OPTIONS,
    BackgroundInfoOptions,
    InterviewSubject,
)
from ainterviewer.types import (
    Feedback,
    Interviewer,
    InterviewStatus,
    LanguageCode,
    MessageRole,
    MessageType,
    TestType,
    TimeDelta,
)
from ainterviewer.utils import now
from app.platform_release import PlatformManifest

from ..types import CollaboratorRole, ExternalParam, ProjectStatus, Scope, TestRunStatus
from ._extra import PydanticJSONB
from .types import AccessRequestStatus, AnnotationType, InterviewType, LanguageType

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

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4, unique=True
    )


############
# Metadata #
############


class PlatformReleaseTable(Base):
    __tablename__ = "platform_release"

    platform_release_version: Mapped[str] = mapped_column(unique=True)
    platform_manifest: Mapped[PlatformManifest] = mapped_column(
        PydanticJSONB(PlatformManifest)
    )
    created_at: Mapped[datetime.datetime] = mapped_column(default=now)


##########
# Access #
##########


class AccessRequestTable(Base):
    __tablename__ = "access_requests"

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
    invitation: Mapped[Optional["InvitationTable"]] = relationship(
        back_populates="access_request",
        cascade="all, delete-orphan",
        uselist=False,
    )


class InvitationTable(Base):
    __tablename__ = "invitation"

    email: Mapped[str | None] = mapped_column(unique=True, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(default=now)
    expires_at: Mapped[datetime.datetime | None] = mapped_column(nullable=True)
    redeemed_at: Mapped[datetime.datetime | None] = mapped_column(default=None)

    reuseable: Mapped[bool] = mapped_column(default=False)
    user_scope: Mapped[Scope] = mapped_column(default=Scope.USER)
    user_expires: Mapped[datetime.datetime | TimeDelta | None] = mapped_column(
        PydanticJSONB(datetime.datetime | TimeDelta | None), default=None
    )
    title: Mapped[str | None] = mapped_column(default=None)

    access_request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("access_requests.id")
    )

    # Relationships
    access_request: Mapped[Optional["AccessRequestTable"]] = relationship(
        back_populates="invitation"
    )


##############
# Newsletter #
##############


class NewsletterSubscriptionTable(Base):
    __tablename__ = "newsletter_subscription"

    email: Mapped[str] = mapped_column(unique=True, index=True)
    created_at: Mapped[datetime.datetime] = mapped_column(default=now)
    unsubscribed_at: Mapped[datetime.datetime | None] = mapped_column(default=None)
    opt_out_token: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), unique=True, default=uuid4
    )


################
# Refresh Token #
################


class RefreshTokenTable(Base):
    __tablename__ = "refresh_token"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(unique=True, index=True)
    family_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)
    is_revoked: Mapped[bool] = mapped_column(default=False)
    is_used: Mapped[bool] = mapped_column(default=False)
    extended: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime.datetime] = mapped_column(default=now)
    expires_at: Mapped[datetime.datetime] = mapped_column()

    # Relationships
    user: Mapped["UserTable"] = relationship(back_populates="refresh_tokens")


########
# User #
########


class UserTable(Base):
    __tablename__ = "user"

    email: Mapped[str] = mapped_column(unique=True)
    password: Mapped[str] = mapped_column()
    first_name: Mapped[str] = mapped_column()
    last_name: Mapped[str | None] = mapped_column()
    created_at: Mapped[datetime.datetime] = mapped_column(default=now)
    last_active: Mapped[datetime.datetime] = mapped_column(default=now)
    last_login: Mapped[datetime.datetime] = mapped_column(default=now)
    scope: Mapped[Scope] = mapped_column(SQLEnum(Scope), default=Scope.USER)
    invite_token: Mapped[uuid.UUID | None] = mapped_column()
    registration_token: Mapped[str | None] = mapped_column(default=None)
    research_consent: Mapped[bool] = mapped_column(default=False)
    invitation_title: Mapped[str | None] = mapped_column(default=None)
    expires_at: Mapped[datetime.datetime | None] = mapped_column(default=None)
    access_request_message: Mapped[str | None] = mapped_column(Text, default=None)
    organization: Mapped[str | None] = mapped_column(default=None)
    admin_note: Mapped[str | None] = mapped_column(Text, default=None)
    admin_note_updated_at: Mapped[datetime.datetime | None] = mapped_column(
        default=None
    )
    with_demo_features: Mapped[bool] = mapped_column(
        default=False, server_default=sa.false()
    )

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
        back_populates="user", cascade="all, delete-orphan"
    )
    experiments: Mapped[list["ExperimentTable"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    owned_projects: Mapped[list["ProjectTable"]] = relationship(
        back_populates="owner",
        foreign_keys="ProjectTable.owner_id",
        cascade="all, delete-orphan",
    )
    refresh_tokens: Mapped[list["RefreshTokenTable"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


#############
# Project #
#############


class ProjectFolderTable(Base):
    __tablename__ = "projectfolder"

    title: Mapped[str] = mapped_column()
    created_at: Mapped[datetime.datetime] = mapped_column(default=now)
    last_updated: Mapped[datetime.datetime | None] = mapped_column(
        default=now, onupdate=now
    )

    # Relationsiphs
    projects: Mapped[list["ProjectTable"]] = relationship(
        back_populates="folder", cascade="all, delete-orphan"
    )
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
    participants: Mapped[list["ParticipantTable"]] = relationship(
        back_populates="folder", cascade="all, delete-orphan"
    )


class ProjectTable(Base):
    __tablename__ = "project"
    __table_args__ = (UniqueConstraint("title", "folder_id", name="title"),)

    folder_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projectfolder.id", ondelete="CASCADE")
    )
    title: Mapped[str] = mapped_column()
    created_at: Mapped[datetime.datetime] = mapped_column(default=now)
    # Also bumped by DB triggers when any ProjectLocalization, TestSetup, or
    # TestRun row changes; see app/db/triggers.py.
    last_updated: Mapped[datetime.datetime | None] = mapped_column(
        default=now, onupdate=now
    )
    status: Mapped[ProjectStatus] = mapped_column(
        SQLEnum(ProjectStatus), default=ProjectStatus.ACTIVE
    )
    config: Mapped[InterviewConfig] = mapped_column(
        PydanticJSONB(InterviewConfig),
        default=InterviewConfig,
    )
    external_params: Mapped[list[ExternalParam] | None] = mapped_column(
        PydanticJSONB(list[ExternalParam]), default=None
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE")
    )

    # Relationships
    owner: Mapped["UserTable"] = relationship(
        back_populates="owned_projects", foreign_keys=[owner_id]
    )
    interviews: Mapped[list["InterviewTable"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    localizations: Mapped[list["ProjectLocalizationTable"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    folder: Mapped["ProjectFolderTable"] = relationship(back_populates="projects")
    tests: Mapped[list["TestSetupTable"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    analysis_categories: Mapped[list["AnalysisCategoryTable"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    experiment_projects: Mapped[list["ExperimentProjectTable"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    experiments = association_proxy(
        "experiment_projects",
        "experiment",
        creator=lambda experiment: ExperimentProjectTable(experiment=experiment),
    )
    project_participants: Mapped[list["ProjectParticipantTable"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    participants = association_proxy(
        "project_participants",
        "participant",
        creator=lambda participant: ProjectParticipantTable(participant=participant),
    )

    @property
    def collaborators(self) -> list["UserTable"]:
        return self.folder.collaborators if self.folder else []


class ProjectLocalizationTable(Base):
    __tablename__ = "projectlocalization"
    __table_args__ = (
        UniqueConstraint("project_id", "language", name="unique_project_language"),
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
    # Changes here also bump ProjectTable.last_updated via a DB trigger;
    # see app/db/triggers.py.
    last_updated: Mapped[datetime.datetime | None] = mapped_column(
        default=now, onupdate=now
    )
    participant_email_subject: Mapped[str | None] = mapped_column(default=None)
    participant_email_template: Mapped[str | None] = mapped_column(default=None)
    participant_reminder_email_subject: Mapped[str | None] = mapped_column(default=None)
    participant_reminder_email_template: Mapped[str | None] = mapped_column(
        default=None
    )

    # Relationships
    project: Mapped["ProjectTable"] = relationship(back_populates="localizations")


################
# Participants #
################


class ParticipantTable(Base):
    """Folder-scoped participant. Holds shared profile/state; linked to projects
    via ProjectParticipantTable so the same person can appear in multiple
    projects within the same folder while sharing fields like email, name,
    participating status, and opt-out reason."""

    __tablename__ = "participant"
    __table_args__ = (
        UniqueConstraint("folder_id", "pid", name="uq_participant_folder_pid"),
    )

    folder_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projectfolder.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str | None] = mapped_column(default=None)
    email: Mapped[str | None] = mapped_column(default=None)
    pid: Mapped[str | None] = mapped_column(default=None)
    lang: Mapped[LanguageCode | None] = mapped_column(None)
    created_at: Mapped[datetime.datetime] = mapped_column(default=now)
    participating: Mapped[bool] = mapped_column(default=True, server_default=sa.true())
    opt_out_reason: Mapped[str | None] = mapped_column(default=None)
    opt_out_token: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), unique=True, default=uuid4
    )

    # Relationships
    folder: Mapped["ProjectFolderTable"] = relationship(back_populates="participants")
    project_participants: Mapped[list["ProjectParticipantTable"]] = relationship(
        back_populates="participant", cascade="all, delete-orphan"
    )
    projects = association_proxy(
        "project_participants",
        "project",
        creator=lambda project: ProjectParticipantTable(project=project),
    )


class ProjectParticipantTable(Base):
    """Join row linking a folder-scoped Participant to a specific Project.
    Per-project state lives here; shared profile state lives on
    ParticipantTable."""

    __tablename__ = "project_participant"
    __table_args__ = (
        UniqueConstraint("project_id", "participant_id", name="uq_project_participant"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), index=True
    )
    participant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("participant.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(default=now)

    # Relationships
    project: Mapped["ProjectTable"] = relationship(
        back_populates="project_participants"
    )
    participant: Mapped["ParticipantTable"] = relationship(
        back_populates="project_participants"
    )
    interviews: Mapped[list["InterviewTable"]] = relationship(
        back_populates="project_participant"
    )

    @hybrid_property
    def latest_interview_at(self) -> datetime.datetime | None:
        candidates = [(i.last_updated or i.created_at) for i in self.interviews]
        return max(candidates) if candidates else None

    @latest_interview_at.expression
    def latest_interview_at(cls):
        return (
            select(
                func.max(
                    func.coalesce(
                        InterviewTable.last_updated, InterviewTable.created_at
                    )
                )
            )
            .where(InterviewTable.participant_id == cls.id)
            .scalar_subquery()
        )

    @hybrid_property
    def latest_interview_status(self) -> InterviewStatus | None:
        if not self.interviews:
            return None
        latest = max(self.interviews, key=lambda i: i.last_updated or i.created_at)
        return latest.status

    @latest_interview_status.expression
    def latest_interview_status(cls):
        return (
            select(InterviewTable.status)
            .where(InterviewTable.participant_id == cls.id)
            .order_by(
                func.coalesce(
                    InterviewTable.last_updated, InterviewTable.created_at
                ).desc()
            )
            .limit(1)
            .scalar_subquery()
        )


#################
# Collaborators #
#################


class CollaboratorTable(Base):
    __tablename__ = "collaborator"
    __table_args__ = (UniqueConstraint("folder_id", "user_id", name="uq_collaborator"),)

    folder_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projectfolder.id"))
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE")
    )
    added_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL")
    )
    added_at: Mapped[datetime.datetime] = mapped_column(default=now)
    role: Mapped[CollaboratorRole] = mapped_column(SQLEnum(CollaboratorRole))

    # Relationships
    folder: Mapped["ProjectFolderTable"] = relationship(
        back_populates="folder_collaborations"
    )
    user: Mapped["UserTable"] = relationship(
        back_populates="folder_collaborations",
        foreign_keys=[user_id],
    )
    added_by: Mapped[Optional["UserTable"]] = relationship(foreign_keys=[added_by_id])


##############
# Experiment #
##############


class ExperimentProjectTable(Base):
    """Association table for many-to-many relationship between experiments and projects."""

    __tablename__ = "experiment_project"
    __table_args__ = (
        UniqueConstraint("experiment_id", "project_id", name="uq_experiment_project"),
    )

    experiment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("experiment.id"))
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("project.id"))
    weight: Mapped[float | None] = mapped_column(default=None)
    added_at: Mapped[datetime.datetime] = mapped_column(default=now)

    # Relationships
    experiment: Mapped["ExperimentTable"] = relationship(
        back_populates="experiment_projects"
    )
    project: Mapped["ProjectTable"] = relationship(back_populates="experiment_projects")


class ExperimentTable(Base):
    __tablename__ = "experiment"

    title: Mapped[str] = mapped_column()
    created_at: Mapped[datetime.datetime] = mapped_column(default=now)
    status: Mapped[ProjectStatus] = mapped_column(
        SQLEnum(ProjectStatus), default=ProjectStatus.ACTIVE
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user.id"))

    # Relationships
    user: Mapped["UserTable"] = relationship(back_populates="experiments")
    experiment_projects: Mapped[list["ExperimentProjectTable"]] = relationship(
        back_populates="experiment", cascade="all, delete-orphan"
    )
    projects = association_proxy(
        "experiment_projects",
        "project",
        creator=lambda project: ExperimentProjectTable(project=project),
    )
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

    interview_guide: Mapped[InterviewGuide] = mapped_column(
        PydanticJSONB(InterviewGuide)
    )
    language: Mapped[LanguageCode] = mapped_column(LanguageType, default="EN")
    type: Mapped[InterviewType] = mapped_column(SQLEnum(InterviewType))
    interviewer: Mapped[Interviewer] = mapped_column(
        SQLEnum(Interviewer), default=Interviewer.AI
    )
    status: Mapped[InterviewStatus] = mapped_column(
        SQLEnum(InterviewStatus), default=InterviewStatus.INACTIVE
    )

    created_at: Mapped[datetime.datetime] = mapped_column(default=now)
    last_updated: Mapped[datetime.datetime | None] = mapped_column(onupdate=now)
    total_time_spent: Mapped[int] = mapped_column(default=0)
    survey_token: Mapped[str | None] = mapped_column()
    user_agent: Mapped[str | None] = mapped_column()
    ip_address: Mapped[str | None] = mapped_column()
    referer: Mapped[str | None] = mapped_column()
    platform_version: Mapped[str | None] = mapped_column(default=None)
    external_params: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("project.id"))
    experiment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("experiment.id", ondelete="SET NULL")
    )
    test_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("testrun.id", ondelete="CASCADE")
    )
    participant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("project_participant.id", ondelete="SET NULL"), default=None
    )

    # Relationships
    project: Mapped["ProjectTable"] = relationship(back_populates="interviews")
    experiment: Mapped[Optional["ExperimentTable"]] = relationship(
        back_populates="interviews"
    )
    test_run: Mapped[Optional["TestRunTable"]] = relationship(
        back_populates="interviews"
    )
    project_participant: Mapped[Optional["ProjectParticipantTable"]] = relationship(
        back_populates="interviews"
    )
    messages: Mapped[list["MessageTable"]] = relationship(
        back_populates="interview", cascade="all, delete-orphan"
    )
    tasks: Mapped[list["TaskTable"]] = relationship(
        back_populates="interview", cascade="all, delete-orphan"
    )
    interviewee: Mapped[list["IntervieweeTable"]] = relationship(
        back_populates="interview", cascade="all, delete-orphan"
    )

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

    @property
    def test_name(self) -> str | None:
        if self.type != InterviewType.SYNTHETIC_TEST or self.test_run is None:
            return None
        return self.test_run.test_setup.name if self.test_run.test_setup else None


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

    message_id: Mapped[int] = mapped_column()
    content: Mapped[str] = mapped_column(Text)
    role: Mapped[MessageRole] = mapped_column(SQLEnum(MessageRole))
    interview_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("interview.id", ondelete="CASCADE")
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE")
    )
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
    # Filename of the audio recording the message was transcribed from,
    # relative to the interview's audio storage directory.
    audio_file: Mapped[str | None] = mapped_column()
    feedback: Mapped[Feedback | None] = mapped_column(SQLEnum(Feedback))
    created_at: Mapped[datetime.datetime] = mapped_column(default=now)
    image: Mapped[Image | None] = mapped_column(PydanticJSONB(Image))
    survey_item: Mapped[SurveyItem | None] = mapped_column(PydanticJSONB(SurveyItem))
    skipped_by_condition: Mapped[bool] = mapped_column(default=False)

    # Relationships
    interview: Mapped["InterviewTable"] = relationship(back_populates="messages")
    annotations: Mapped[list["MessageAnnotationTable"]] = relationship(
        back_populates="message", cascade="all, delete-orphan"
    )

    @hybrid_property
    def interview_type(self) -> Optional[InterviewType]:
        return self.interview.type if self.interview else None

    @interview_type.expression
    def interview_type(cls):
        return (
            select(InterviewTable.type)
            .where(InterviewTable.id == cls.interview_id)
            .scalar_subquery()
        )


########
# Task #
########


class TaskTable(Base):
    __tablename__ = "task"

    created_at: Mapped[datetime.datetime] = mapped_column(default=now)
    message_id: Mapped[int] = mapped_column()
    interview_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("interview.id", ondelete="CASCADE")
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE")
    )
    task: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    context: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)
    response: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column()
    time_spend: Mapped[int | None] = mapped_column()

    # Relationships
    interview: Mapped["InterviewTable"] = relationship(back_populates="tasks")


###################
# Synthetic Tests #
###################


class TestSetupTable(Base):
    __tablename__ = "testsetup"

    name: Mapped[str | None] = mapped_column()
    type: Mapped[TestType] = mapped_column(SQLEnum(TestType))
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("project.id"))
    created_at: Mapped[datetime.datetime] = mapped_column(default=now)
    # Also bumped by a DB trigger when any TestRun row for this setup changes,
    # and changes here bump ProjectTable.last_updated; see app/db/triggers.py.
    last_updated: Mapped[datetime.datetime | None] = mapped_column(onupdate=now)
    language: Mapped[LanguageCode] = mapped_column(LanguageType, default="EN")
    n_interviews: Mapped[int] = mapped_column(default=5)
    answering_model: Mapped[str] = mapped_column()
    delay_before_answers: Mapped[Any | None] = mapped_column(JSON)
    background_info: Mapped[BackgroundInfoOptions] = mapped_column(
        PydanticJSONB(BackgroundInfoOptions),
        default=DEFAULT_BACKGROUND_INFO_OPTIONS,
    )
    fixed_answers: Mapped[list[str] | None] = mapped_column(JSON)
    fixed_personas: Mapped[list[str] | None] = mapped_column(JSON)

    # Relationships
    test_runs: Mapped[list["TestRunTable"]] = relationship(
        back_populates="test_setup", cascade="all, delete-orphan"
    )
    project: Mapped["ProjectTable"] = relationship(back_populates="tests")

    @hybrid_property
    def n_runs(self) -> int:
        """Get the number of test runs for a test setup"""
        return len(self.test_runs)


class TestRunTable(Base):
    __tablename__ = "testrun"

    test_setup_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("testsetup.id"))
    language: Mapped[LanguageCode] = mapped_column(LanguageType, default="EN")
    n_interviews: Mapped[int] = mapped_column()
    answering_model: Mapped[str | None] = mapped_column()
    delay_before_answers: Mapped[Any | None] = mapped_column(JSON)
    created_at: Mapped[datetime.datetime] = mapped_column(default=now)
    # Changes here also bump TestSetupTable.last_updated and
    # ProjectTable.last_updated via DB triggers; see app/db/triggers.py.
    last_updated: Mapped[datetime.datetime | None] = mapped_column(onupdate=now)
    status: Mapped[TestRunStatus] = mapped_column(
        SQLEnum(TestRunStatus), default=TestRunStatus.PENDING
    )

    # Relationships
    test_setup: Mapped["TestSetupTable"] = relationship(back_populates="test_runs")
    interviews: Mapped[list["InterviewTable"]] = relationship(
        back_populates="test_run", cascade="all, delete-orphan"
    )


class IntervieweeTable(Base):
    __tablename__ = "interviewee"

    interview_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("interview.id", ondelete="CASCADE")
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE")
    )
    interview_subject: Mapped[InterviewSubject | str] = mapped_column(
        PydanticJSONB(InterviewSubject | str)
    )

    # Relationships
    interview: Mapped["InterviewTable"] = relationship(back_populates="interviewee")


############
# Analysis #
############


class AnalysisCategoryTable(Base):
    __tablename__ = "analysis_category"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="unique_project_category_name"),
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

    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("message.id", ondelete="CASCADE")
    )
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

    annotation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("message_annotation.id")
    )
    category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("analysis_category.id"))
    value_int: Mapped[int] = mapped_column()

    # Relationships
    annotation: Mapped["MessageAnnotationTable"] = relationship(back_populates="values")
    category: Mapped["AnalysisCategoryTable"] = relationship(back_populates="values")


##############
# Assistance #
##############


class AssistanceSessionTable(Base):
    __tablename__ = "assistance_session"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(default=now)

    # Relationships
    message_chunks: Mapped[list["AssistanceMessageChunkTable"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class AssistanceMessageChunkTable(Base):
    """One row per agent run — stores the raw JSON from new_messages_json()."""

    __tablename__ = "assistance_message_chunk"

    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assistance_session.id", ondelete="CASCADE")
    )
    messages_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(default=now)

    # Relationships
    session: Mapped["AssistanceSessionTable"] = relationship(
        back_populates="message_chunks"
    )
