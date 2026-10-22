"""Writes for a deleted interview, with foreign keys enforced.

An in-memory SQLite database with the app's schema and `foreign_keys=ON`, the
way `register_pragmas` sets it up for every pooled connection.
"""

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ainterviewer.interview_guides import InterviewGuide
from ainterviewer.types import InterviewStatus, MessageRole
from app.db.crud import InterviewDataBase
from app.db.repositories.errors import InterviewGoneError
from app.db.tables import (
    Base,
    InterviewTable,
    ProjectFolderTable,
    ProjectTable,
    UserTable,
)
from app.db.utils import is_foreign_key_violation
from app.types import InterviewType


@pytest.fixture
def db():
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def _enforce(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        user = UserTable(email="a@example.com", password="x", first_name="A")
        folder = ProjectFolderTable(title="f")
        project = ProjectTable(title="p", folder=folder, owner=user)
        interview = InterviewTable(
            project=project,
            interview_guide=InterviewGuide(),
            type=InterviewType.SYNTHETIC_TEST,
        )
        session.add_all([user, folder, project, interview])
        session.commit()
        yield InterviewDataBase(session), project.id, interview.id


def _message(db: InterviewDataBase, project_id, interview_id, message_id: int):
    return db.interviews.insert_message(
        message_id=message_id,
        content="hi",
        role=MessageRole.USER,
        interview_id=interview_id,
        project_id=project_id,
    )


def _delete(db: InterviewDataBase, project_id, interview_id):
    db.interviews.delete_interviews(project_id, [interview_id])


def test_duplicate_message_is_still_skipped(db):
    database, project_id, interview_id = db
    _message(database, project_id, interview_id, 1)
    assert _message(database, project_id, interview_id, 1) == 1


def test_message_for_deleted_interview_raises(db):
    database, project_id, interview_id = db
    _delete(database, project_id, interview_id)
    with pytest.raises(InterviewGoneError):
        _message(database, project_id, interview_id, 1)


def test_task_for_deleted_interview_raises(db):
    database, project_id, interview_id = db
    _delete(database, project_id, interview_id)
    with pytest.raises(InterviewGoneError):
        database.interviews.insert_task(
            message_id=1,
            interview_id=interview_id,
            project_id=project_id,
            task="t",
        )


def test_session_is_usable_after_the_failed_write(db):
    """AInterviewer.__aexit__ updates the interview's status on the way out; a
    session left in a failed transaction would raise there and mask the error."""
    database, project_id, interview_id = db
    _delete(database, project_id, interview_id)
    with pytest.raises(InterviewGoneError):
        _message(database, project_id, interview_id, 1)
    database.interviews.update_interview_status(
        project_id, interview_id, status=InterviewStatus.INACTIVE
    )


# A task pointing at an interview that does not exist.
_ORPHAN_TASK = (
    "INSERT INTO task (id, message_id, interview_id, project_id, task, "
    "created_at) VALUES ('t1', 1, 'missing', 'missing', 'x', '2026-01-01')"
)
# A second user with the first one's email.
_DUPLICATE_EMAIL = (
    "INSERT INTO user (id, email, password, first_name, email_verified, "
    "two_factor_enabled, created_at, last_active, last_login, scope, "
    "research_consent, with_preview_features) SELECT 'u2', email, "
    "password, first_name, 0, 1, created_at, last_active, last_login, "
    "scope, 0, 0 FROM user"
)


@pytest.mark.parametrize(
    ("statement", "expected"), [(_ORPHAN_TASK, True), (_DUPLICATE_EMAIL, False)]
)
def test_classifier_tells_foreign_key_from_unique(db, statement, expected):
    session = db[0].interviews.session
    with pytest.raises(IntegrityError) as caught:
        session.connection().exec_driver_sql(statement)
    session.rollback()
    assert is_foreign_key_violation(caught.value) is expected
