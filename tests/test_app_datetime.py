"""Datetime columns read back aware and honour the zone of what they are given.

The columns store naive app-time-zone wall clock; see `AppDateTime`.
"""

import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ainterviewer.interview_guides import InterviewGuide
from ainterviewer.settings import settings as lib_settings
from app.db.crud import InterviewDataBase
from app.db.tables import (
    Base,
    InterviewTable,
    ProjectFolderTable,
    ProjectTable,
    UserTable,
)
from app.types import InterviewType

APP_TZ = lib_settings.tzinfo
UTC = ZoneInfo("UTC")
# 01:00 in Copenhagen on 21 August is 23:00 UTC on the 20th: the hour a
# filter that drops offsets puts on the wrong day.
CREATED = datetime.datetime(2026, 8, 21, 1, 0, tzinfo=APP_TZ)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        user = UserTable(email="a@example.com", password="x", first_name="A")
        folder = ProjectFolderTable(title="f")
        project = ProjectTable(title="p", folder=folder, owner=user)
        interview = InterviewTable(
            project=project,
            interview_guide=InterviewGuide(),
            type=InterviewType.DISTRIBUTED,
            created_at=CREATED.astimezone(UTC),
        )
        session.add_all([user, folder, project, interview])
        session.commit()
        yield InterviewDataBase(session), project.id, interview.id


def test_values_read_back_aware_in_the_app_zone(db):
    database, _, interview_id = db
    database.session.expire_all()
    created = database.session.get_one(InterviewTable, interview_id).created_at
    # Written in UTC, stored as app wall clock, the same instant either way.
    assert created.tzinfo is not None
    assert created == CREATED
    assert created.utcoffset() == datetime.timedelta(hours=2)


@pytest.mark.parametrize(
    ("created_from", "included"),
    [
        # Midnight in Copenhagen, sent as UTC the way the browser sends it.
        (datetime.datetime(2026, 8, 20, 22, 0, tzinfo=UTC), True),
        # Midnight UTC is 02:00 in Copenhagen, after the interview.
        (datetime.datetime(2026, 8, 21, 0, 0, tzinfo=UTC), False),
    ],
)
def test_filters_compare_instants_not_wall_clocks(db, created_from, included):
    database, project_id, interview_id = db
    ids = database.interviews.get_interview_ids(project_id, created_from=created_from)
    assert (interview_id in ids) is included


def test_the_api_sends_an_offset(db):
    database, project_id, interview_id = db
    summary = database.interviews.get_interview_summaries(project_id, [interview_id])[
        interview_id
    ]
    assert '"created_at":"2026-08-21T01:00:00+02:00"' in summary.model_dump_json()
