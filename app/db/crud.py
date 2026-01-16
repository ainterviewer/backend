from pathlib import Path

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text
from sqlalchemy.orm import Session

from ainterviewer.interfaces import PersistenceProtocol

from ..paths import APP_DIR
from .repositories import (
    AnalysisRepository,
    InterviewRepository,
    ProjectRepository,
    TestRepository,
    UserRepository,
)
from .tables import Base

ALEMBIC_BASE_RIVISON_ID = "fbcbd179bfba"


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
        self.session = session
        self._alembic_config = AlembicConfig(APP_DIR.parent / "alembic.ini")

        # Initialize all repositories with the shared session
        self.users: UserRepository = UserRepository(session)
        self.projects: ProjectRepository = ProjectRepository(session)
        self.interviews: InterviewRepository = InterviewRepository(session)
        self.tests: TestRepository = TestRepository(session)
        self.analysis: AnalysisRepository = AnalysisRepository(session)

    # ==================== Database Management ====================

    def create_db_and_tables(self):
        """Creates the db and all the required tables based on the SQLAlchemy models"""

        alembic_init_head_migration = list(
            Path(APP_DIR.parent / "alembic" / "versions").glob(
                "*" + ALEMBIC_BASE_RIVISON_ID + "*.py"
            )
        )

        self.session.execute(text("PRAGMA foreign_keys=ON"))
        self.session.execute(text("PRAGMA journal_mode=WAL"))
        self.session.execute(text("PRAGMA busy_timeout=60000"))
        self.session.execute(text("PRAGMA cache_size=-65536"))
        self.session.execute(text("PRAGMA temp_store=MEMORY"))

        Base.metadata.create_all(self.session.bind)

        if alembic_init_head_migration:
            alembic_command.stamp(self._alembic_config, ALEMBIC_BASE_RIVISON_ID)
        else:
            alembic_command.revision(
                self._alembic_config,
                autogenerate=True,
                rev_id=ALEMBIC_BASE_RIVISON_ID,
            )
            alembic_command.upgrade(self._alembic_config, "head")

    def drop_all_tables(self):
        """Drops all tables - useful for testing or complete reset"""
        Base.metadata.drop_all(self.session.bind)

    # ==================== PersistenceProtocol Implementation ====================
    # These methods delegate to the InterviewRepository to satisfy the protocol

    def create_interview(self, *args, **kwargs):
        return self.interviews.create_interview(*args, **kwargs)

    def get_interview(self, *args, **kwargs):
        return self.interviews.get_interview(*args, **kwargs)

    def update_interview_status(self, *args, **kwargs):
        return self.interviews.update_interview_status(*args, **kwargs)

    def insert_message(self, *args, **kwargs):
        return self.interviews.insert_message(*args, **kwargs)

    def save_image(self, *args, **kwargs):
        return self.interviews.save_image(*args, **kwargs)

    def insert_task(self, *args, **kwargs):
        return self.interviews.insert_task(*args, **kwargs)
