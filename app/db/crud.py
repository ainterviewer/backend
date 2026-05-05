from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from ainterviewer.interfaces import PersistenceProtocol
from app.platform_release import PlatformManifest

from ..paths import APP_DIR
from .repositories import (
    AnalysisRepository,
    AssistanceRepository,
    AuthRepository,
    InterviewRepository,
    ParticipantRepository,
    ProjectRepository,
    TestRepository,
    UserRepository,
)
from .tables import Base, PlatformReleaseTable
from .triggers import install_triggers


class InterviewDataBase(PersistenceProtocol):
    """
    Facade class providing access to all database repositories.

    This class implements the PersistenceProtocol and delegates operations
    to specialized repositories. All repositories share the same session,
    ensuring transactional consistency.

    Usage:
        db = InterviewDataBase(session)

        # Access repositories directly
        user = db.users.create_user(...)
        project = db.projects.create_project(...)
        interview = db.interviews.create_interview(...)
    """

    def __init__(self, session: Session):
        self.session: Session = session
        self._alembic_config: AlembicConfig = AlembicConfig(
            APP_DIR.parent / "alembic.ini"
        )

        # Initialize all repositories with the shared session
        self.auth: AuthRepository = AuthRepository(session)
        self.users: UserRepository = UserRepository(session)
        self.projects: ProjectRepository = ProjectRepository(session)
        self.interviews: InterviewRepository = InterviewRepository(session)
        self.participants: ParticipantRepository = ParticipantRepository(session)
        self.tests: TestRepository = TestRepository(session)
        self.analysis: AnalysisRepository = AnalysisRepository(session)
        self.assistance: AssistanceRepository = AssistanceRepository(session)

    # ==================== Database Management ====================

    def create_db_and_tables(self):
        """Creates the db and all the required tables based on the SQLAlchemy models"""
        self.session.execute(text("PRAGMA foreign_keys=ON"))
        self.session.execute(text("PRAGMA journal_mode=WAL"))
        self.session.execute(text("PRAGMA wal_autocheckpoint=100"))
        self.session.execute(text("PRAGMA busy_timeout=60000"))
        self.session.execute(text("PRAGMA cache_size=-65536"))
        self.session.execute(text("PRAGMA temp_store=MEMORY"))

        Base.metadata.create_all(self.session.connection())
        install_triggers(self.session.connection())
        alembic_command.stamp(self._alembic_config, "head")

    def drop_all_tables(self):
        """Drops all tables - useful for testing or complete reset"""
        Base.metadata.drop_all(self.session.connection())

    def on_startup(self):
        self.interviews.change_active_to_inactive()

    def on_shutdown(self) -> None:
        if (
            isinstance(self.session.bind, Engine)
            and self.session.bind.dialect.name == "sqlite"
        ):
            self.session.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
            self.session.commit()

    # ==================== Metadata ====================

    def get_platform_release(
        self, platform_version: str | None = None
    ) -> PlatformManifest:
        statement = select(PlatformReleaseTable)

        if platform_version is not None:
            statement = statement.where(
                PlatformReleaseTable.platform_release_version == platform_version
            )
            platform_release_entry = self.session.execute(statement).scalar_one()
        else:
            statement = statement.order_by(
                PlatformReleaseTable.created_at.desc()
            ).limit(1)
            platform_release_entry = self.session.execute(statement).scalar_one()

        return platform_release_entry.platform_manifest

    def set_platform_release(self, platform_manifest: PlatformManifest):
        platform_release_entry = PlatformReleaseTable(
            platform_release_version=platform_manifest.platform_version,
            platform_manifest=platform_manifest,
        )
        self.session.add(platform_release_entry)
        self.session.commit()
        self.session.refresh(platform_release_entry)
        return platform_manifest

    # ==================== PersistenceProtocol Implementation ====================
    # These methods delegate to the InterviewRepository to satisfy the protocol

    def create_interview(self, *args, **kwargs):
        return self.interviews.create_interview(*args, **kwargs)

    def get_interview(self, *args, **kwargs):
        return self.interviews.get_interview(*args, **kwargs)

    def update_interview_status(self, *args, **kwargs):
        return self.interviews.update_interview_status(*args, **kwargs)

    def update_interview_guide(self, *args, **kwargs):
        return self.interviews.update_interview_guide(*args, **kwargs)

    def insert_message(self, *args, **kwargs):
        return self.interviews.insert_message(*args, **kwargs)

    def save_image(self, *args, **kwargs):
        return self.interviews.save_image(*args, **kwargs)

    def insert_task(self, *args, **kwargs):
        return self.interviews.insert_task(*args, **kwargs)
