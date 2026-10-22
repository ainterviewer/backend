"""The admin overview: how activity is recorded, and how it is counted.

Recording is driven through the app, since what matters is the wiring -- that
a scope-checked request writes a `user_activity` row and a token refresh does
not, and that the in-process debounce keeps repeat requests off the database.
The arithmetic is tested on `build_admin_stats` directly, with plain rows.
"""

import datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ainterviewer.settings import settings as lib_settings
from app import dependencies
from app.auth import AuthToken
from app.db.crud import InterviewDataBase
from app.db.repositories.stats import (
    ActivityRow,
    InterviewDayRow,
    ProjectDayRow,
    UserRow,
)
from app.db.tables import Base, UserActivityTable, UserTable
from app.dependencies import get_db
from app.main import app
from app.services.admin_stats import build_admin_stats
from app.types import InterviewType, Scope

TZ = lib_settings.tzinfo


@pytest.fixture
def setup():
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _enforce(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        admin = UserTable(
            email="a@example.com", password="x", first_name="Ada", scope=Scope.ADMIN
        )
        session.add(admin)
        session.commit()
        admin_id = admin.id

    def _db():
        with Session(engine) as session:
            yield InterviewDataBase(session)

    app.dependency_overrides[get_db] = _db
    dependencies._activity_recorded.clear()
    token = AuthToken(user_id=admin_id, scope=Scope.ADMIN).encode()
    yield TestClient(app), {"access_token": token}, admin_id, engine
    app.dependency_overrides.clear()
    dependencies._activity_recorded.clear()


def _activity(engine) -> list[UserActivityTable]:
    with Session(engine) as session:
        return list(session.scalars(select(UserActivityTable)))


def test_a_scope_checked_request_records_the_day_once(setup):
    client, cookies, admin_id, engine = setup

    assert client.get("/api/admin/stats", cookies=cookies).status_code == 200
    [row] = _activity(engine)
    assert row.user_id == admin_id
    assert row.day == datetime.datetime.now(TZ).date()
    first_seen = row.first_seen

    # Debounced in-process: a second request is not another write.
    client.get("/api/admin/stats", cookies=cookies)
    [row] = _activity(engine)
    assert row.last_seen == first_seen

    # Once the debounce has passed, the same row moves forward.
    dependencies._activity_recorded[admin_id] -= datetime.timedelta(hours=1)
    client.get("/api/admin/stats", cookies=cookies)
    [row] = _activity(engine)
    assert row.first_seen == first_seen
    assert row.last_seen > first_seen

    with Session(engine) as session:
        user = session.get(UserTable, admin_id)
        assert user is not None
        assert user.last_active == row.last_seen


def test_activity_of_a_deleted_user_does_not_fail_the_request(setup):
    client, _, _, _ = setup
    ghost_id = uuid4()
    ghost = AuthToken(user_id=ghost_id, scope=Scope.ADMIN).encode()
    # The foreign key refuses the row; the request still goes through.
    response = client.get("/api/admin/stats", cookies={"access_token": ghost})
    assert response.status_code == 200
    # And the failed write gives its debounce slot back, so the next request
    # tries again instead of waiting the window out.
    assert ghost_id not in dependencies._activity_recorded


def test_a_role_checked_request_records_the_day(setup):
    client, cookies, admin_id, engine = setup
    # Gated by `ProjectViewer` alone, with no scope token.
    client.get(f"/api/projects/{uuid4()}/tests", cookies=cookies)
    [row] = _activity(engine)
    assert row.user_id == admin_id


def test_stats_are_admin_only(setup):
    client, _, _, _ = setup
    user = AuthToken(user_id=uuid4(), scope=Scope.USER).encode()
    response = client.get("/api/admin/stats", cookies={"access_token": user})
    assert response.status_code == 403


def _at(day: datetime.date) -> datetime.datetime:
    return datetime.datetime.combine(day, datetime.time(12), TZ)


TODAY = datetime.date(2026, 10, 16)


def _days_ago(n: int) -> datetime.date:
    return TODAY - datetime.timedelta(days=n)


def test_accounts_accumulate_and_expiry_takes_demo_accounts_out_of_usable():
    old = UserRow(uuid4(), Scope.USER, _at(_days_ago(30)), None)
    demo = UserRow(uuid4(), Scope.DEMO, _at(_days_ago(5)), _at(_days_ago(2)))
    stats = build_admin_stats(
        users=[old, demo],
        activity=[],
        interviews=[],
        projects=[],
        start=_days_ago(6),
        today=TODAY,
    )
    by_day = {d.day: d for d in stats.days}

    assert len(stats.days) == 7
    # The user created before the range is in the baseline, not a signup.
    assert by_day[_days_ago(6)].accounts.user == 1
    assert sum(d.signups.user for d in stats.days) == 0
    assert by_day[_days_ago(5)].signups.demo == 1
    assert by_day[_days_ago(3)].usable.demo == 1
    assert by_day[_days_ago(2)].usable.demo == 0
    assert by_day[_days_ago(2)].accounts.demo == 1
    assert stats.summary.accounts.demo == 1
    assert stats.summary.usable.demo == 0


def test_weekly_active_counts_each_user_once_over_seven_days():
    user = UserRow(uuid4(), Scope.USER, _at(_days_ago(60)), None)
    activity = [ActivityRow(user.id, _days_ago(n)) for n in (0, 1, 3, 8)]
    # Activity of a user who no longer exists is dropped.
    activity.append(ActivityRow(uuid4(), TODAY))
    stats = build_admin_stats(
        users=[user],
        activity=activity,
        interviews=[],
        projects=[],
        start=_days_ago(9),
        today=TODAY,
    )
    by_day = {d.day: d for d in stats.days}

    assert by_day[TODAY].active.user == 1
    assert by_day[TODAY].weekly_active.user == 1
    assert by_day[_days_ago(2)].active.user == 0
    assert by_day[_days_ago(2)].weekly_active.user == 1
    assert stats.summary.active_today.user == 1
    assert stats.summary.active_7d.user == 1
    assert stats.summary.active_30d.user == 1


def test_retention_only_counts_weeks_that_are_over():
    signup = _days_ago(30)
    back = UserRow(uuid4(), Scope.USER, _at(signup), None)
    gone = UserRow(uuid4(), Scope.USER, _at(signup), None)
    activity = [ActivityRow(back.id, signup + datetime.timedelta(days=8))]
    stats = build_admin_stats(
        users=[back, gone],
        activity=activity,
        interviews=[],
        projects=[],
        start=TODAY,
        today=TODAY,
    )
    [cohort] = stats.retention
    weeks = {w.week: w for w in cohort.weeks}

    assert cohort.size == 2
    assert (weeks[1].eligible, weeks[1].retained) == (2, 1)
    assert (weeks[2].eligible, weeks[2].retained) == (2, 0)
    # Week 4 is days 28-34 after signup: not over yet, so nobody is eligible.
    assert weeks[4].eligible == 0


def test_interviews_and_projects_are_counted_per_day():
    stats = build_admin_stats(
        users=[],
        activity=[],
        interviews=[
            InterviewDayRow(TODAY, InterviewType.DISTRIBUTED, 3),
            InterviewDayRow(TODAY, InterviewType.SYNTHETIC_TEST, 5),
        ],
        projects=[ProjectDayRow(TODAY, 2)],
        start=_days_ago(1),
        today=TODAY,
    )
    [yesterday, today] = stats.days
    assert today.interviews.distributed == 3
    assert today.interviews.synthetic_test == 5
    assert today.interviews.manual_test == 0
    assert today.projects == 2
    assert yesterday.projects == 0
