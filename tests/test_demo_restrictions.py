"""What demo accounts may not do on routes they otherwise share with users.

They see test interviews only: the test results page reads them through the
same list, transcript and delete routes as the interview data page, so the
line is drawn by interview type rather than by route. And they may not
publish, since what is published is what respondents get.

Each refusal is `demo_restricted`, which the dashboard holds behind the
restriction overlay instead of showing as an error.
"""

import asyncio
import uuid
from typing import cast

import pytest
from fastapi import Request
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ainterviewer.interview_guides import InterviewGuide
from app.api.dashboard.projects import (
    delete_interviews,
    get_interview_messages,
    get_interviews,
)
from app.api.dashboard.versions import save_content_version
from app.auth import AuthToken
from app.db.crud import InterviewDataBase
from app.db.tables import (
    Base,
    InterviewTable,
    ProjectFolderTable,
    ProjectTable,
    UserTable,
)
from app.errors import AppError
from app.schemas.common import PageParams
from app.schemas.content import ContentVersionCreate
from app.schemas.interviews import InterviewFilterParams, InterviewSelection
from app.types import ErrorCode, InterviewType, Scope

DEMO = AuthToken(user_id=uuid.uuid4(), scope=Scope.DEMO)
USER = AuthToken(user_id=uuid.uuid4(), scope=Scope.USER)
TESTS = [InterviewType.MANUAL_TEST, InterviewType.SYNTHETIC_TEST]


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        user = UserTable(email="a@example.com", password="x", first_name="A")
        folder = ProjectFolderTable(title="f")
        project = ProjectTable(title="p", folder=folder, owner=user)
        session.add_all([user, folder, project])
        session.commit()
        yield InterviewDataBase(session), project.id


def add_interview(db, project_id, type) -> uuid.UUID:
    database, _ = db
    row = InterviewTable(
        project_id=project_id, interview_guide=InterviewGuide(), type=type
    )
    database.session.add(row)
    database.session.commit()
    return row.id


def refused_for_demo(call) -> None:
    with pytest.raises(AppError) as refused:
        asyncio.run(call)
    assert refused.value.status == 403
    assert refused.value.code == ErrorCode.DEMO_RESTRICTED


def listing(db, jwt, interview_types=None):
    database, project_id = db
    return get_interviews(
        project_id=project_id,
        db=database,
        jwt=jwt,
        _=jwt,
        paginated_query=PageParams(),
        interview_filters=InterviewFilterParams(interview_types=interview_types),
    )


class TestTheInterviewList:
    def test_a_demo_account_is_refused_the_distributed_default(self, db):
        refused_for_demo(listing(db, DEMO))

    def test_a_demo_account_is_refused_a_scope_including_distributed(self, db):
        refused_for_demo(listing(db, DEMO, [*TESTS, InterviewType.DISTRIBUTED]))

    def test_a_demo_account_lists_test_interviews(self, db):
        add_interview(db, db[1], InterviewType.MANUAL_TEST)

        assert asyncio.run(listing(db, DEMO, TESTS)).total == 1

    def test_a_user_lists_distributed_interviews(self, db):
        add_interview(db, db[1], InterviewType.DISTRIBUTED)

        assert asyncio.run(listing(db, USER)).total == 1


def transcript(db, jwt, interview_id):
    database, project_id = db
    return get_interview_messages(
        request=cast(Request, None),  # unused by the route
        project_id=project_id,
        interview_id=interview_id,
        db=database,
        jwt=jwt,
        _=jwt,
    )


class TestTheTranscript:
    def test_a_demo_account_is_refused_a_distributed_one(self, db):
        interview_id = add_interview(db, db[1], InterviewType.DISTRIBUTED)

        refused_for_demo(transcript(db, DEMO, interview_id))

    def test_a_demo_account_reads_a_test_one(self, db):
        interview_id = add_interview(db, db[1], InterviewType.SYNTHETIC_TEST)

        assert asyncio.run(transcript(db, DEMO, interview_id)) == []

    def test_a_user_reads_a_distributed_one(self, db):
        interview_id = add_interview(db, db[1], InterviewType.DISTRIBUTED)

        assert asyncio.run(transcript(db, USER, interview_id)) == []


class TestDeleting:
    def test_a_demo_account_deletes_nothing_when_a_distributed_one_is_picked(self, db):
        database, project_id = db
        picked = [
            add_interview(db, project_id, InterviewType.MANUAL_TEST),
            add_interview(db, project_id, InterviewType.DISTRIBUTED),
        ]

        refused_for_demo(
            delete_interviews(
                delete_request=InterviewSelection(interview_ids=picked),
                project_id=project_id,
                db=database,
                jwt=DEMO,
                _=DEMO,
            )
        )
        assert len(database.session.scalars(select(InterviewTable)).all()) == 2


class TestPublishing:
    def test_a_demo_account_may_not_publish(self, db):
        database, project_id = db

        refused_for_demo(
            save_content_version(
                project_id=project_id,
                request=ContentVersionCreate(publish=True),
                db=database,
                jwt=DEMO,
                _=DEMO,
                revision=0,
            )
        )
