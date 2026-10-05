"""Demo accounts get a small budget of synthetic interviews in flight at once,
plus a cap on how many they may start per rolling 24 hours and in total.

Every synthetic interview is a full interview against the shared inference
server, so the budget is counted per user who pressed Run, across all of their
runs, and only over runs that are still pending or running. A run left in
RUNNING by a backend restart must stop counting eventually, or it would lock the
user out for good.

Runs against an in-memory SQLite database.
"""

import asyncio
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ainterviewer.types import TestType
from ainterviewer.utils import now
from app.api.dashboard.synthesize import DEMO_DAILY_WINDOW, run_synthetic_test
from app.api.request_models import SynthesizeRequest
from app.db.repositories.test import TestRepository
from app.db.tables import Base, TestRunTable, TestSetupTable
from app.settings import app_settings
from app.types import Scope, TestRunStatus

LIMITS = app_settings.app.demo_limits
DEMO_ACTIVE_RUN_MAX_AGE = LIMITS.active_run_max_age.to_timedelta()

PROJECT = uuid.uuid4()


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def setup(session) -> TestSetupTable:
    test_setup = TestSetupTable(
        id=uuid.uuid4(),
        project_id=PROJECT,
        type=TestType.FIXED_ANSWERS,
        answering_model="gpt-oss-120b",
        fixed_answers=["Yes."],
    )
    session.add(test_setup)
    session.commit()
    return test_setup


def run(session, setup, user, n, status=TestRunStatus.RUNNING, age=timedelta()):
    session.add(
        TestRunTable(
            id=uuid.uuid4(),
            test_setup_id=setup.id,
            n_interviews=n,
            answering_model="gpt-oss-120b",
            status=status,
            started_by_id=user,
            created_at=now() - age,
        )
    )
    session.commit()


def active(session, user):
    return TestRepository(session).count_active_synthetic_interviews(
        user, max_age=DEMO_ACTIVE_RUN_MAX_AGE
    )


def start(session, setup, user, scope, n):
    endpoint = run_synthetic_test(
        request=SimpleNamespace(cookies={"access_token": "token"}),
        project_id=PROJECT,
        test_id=setup.id,
        request_data=SynthesizeRequest(n_interviews=n, answering_model="gpt-oss-120b"),
        background_tasks=BackgroundTasks(),
        db=SimpleNamespace(tests=TestRepository(session)),
        jwt=SimpleNamespace(user_id=user, scope=scope),
    )
    return asyncio.run(endpoint)


class TestCountingActiveInterviews:
    def test_it_sums_the_runs_still_in_flight(self, session, setup):
        user = uuid.uuid4()
        run(session, setup, user, 3, status=TestRunStatus.PENDING)
        run(session, setup, user, 4, status=TestRunStatus.RUNNING)

        assert active(session, user) == 7

    def test_finished_runs_do_not_count(self, session, setup):
        user = uuid.uuid4()
        run(session, setup, user, 5, status=TestRunStatus.COMPLETED)
        run(session, setup, user, 5, status=TestRunStatus.FAILED)

        assert active(session, user) == 0

    def test_only_the_user_s_own_runs_count(self, session, setup):
        user, someone_else = uuid.uuid4(), uuid.uuid4()
        run(session, setup, someone_else, 8)
        run(session, setup, None, 8)

        assert active(session, user) == 0

    def test_a_run_orphaned_in_running_stops_counting(self, session, setup):
        user = uuid.uuid4()
        run(session, setup, user, 9, age=DEMO_ACTIVE_RUN_MAX_AGE + timedelta(minutes=1))

        assert active(session, user) == 0


class TestTheDemoLimit:
    def test_a_demo_user_cannot_go_over_the_budget(self, session, setup):
        user = uuid.uuid4()
        run(session, setup, user, LIMITS.max_concurrent_synthetic_interviews - 2)

        with pytest.raises(HTTPException) as error:
            start(session, setup, user, Scope.DEMO, 3)

        assert error.value.status_code == 429
        assert session.query(TestRunTable).count() == 1

    def test_a_demo_user_can_fill_the_budget_exactly(self, session, setup):
        user = uuid.uuid4()
        run(session, setup, user, LIMITS.max_concurrent_synthetic_interviews - 2)

        start(session, setup, user, Scope.DEMO, 2)

        started = (
            session.query(TestRunTable).order_by(TestRunTable.created_at).all()[-1]
        )
        assert started.started_by_id == user

    def test_a_single_run_over_the_budget_is_refused(self, session, setup):
        with pytest.raises(HTTPException) as error:
            start(
                session,
                setup,
                uuid.uuid4(),
                Scope.DEMO,
                LIMITS.max_concurrent_synthetic_interviews + 1,
            )

        assert error.value.status_code == 429

    def test_other_users_are_not_limited(self, session, setup):
        user = uuid.uuid4()
        run(session, setup, user, 50)

        start(session, setup, user, Scope.USER, 50)

        assert active(session, user) == 100


def fill(session, setup, user, total, status=TestRunStatus.COMPLETED, age=timedelta()):
    """Record finished runs adding up to `total` interviews, none over the
    concurrent budget, so only the daily or lifetime limit can be hit."""
    while total > 0:
        n = min(total, LIMITS.max_concurrent_synthetic_interviews)
        run(session, setup, user, n, status=status, age=age)
        total -= n


class TestCountingStartedInterviews:
    def test_every_status_counts(self, session, setup):
        user = uuid.uuid4()
        for status in TestRunStatus:
            run(session, setup, user, 2, status=status)

        assert TestRepository(session).count_started_synthetic_interviews(
            user
        ) == 2 * len(TestRunStatus)

    def test_since_leaves_out_older_runs(self, session, setup):
        user = uuid.uuid4()
        run(session, setup, user, 3, age=timedelta(days=2))
        run(session, setup, user, 4)

        repo = TestRepository(session)
        assert repo.count_started_synthetic_interviews(user) == 7
        assert (
            repo.count_started_synthetic_interviews(
                user, since=now() - DEMO_DAILY_WINDOW
            )
            == 4
        )


class TestTheDailyAndLifetimeLimits:
    def test_the_daily_limit_refuses_a_run_that_would_cross_it(self, session, setup):
        user = uuid.uuid4()
        fill(session, setup, user, LIMITS.daily_synthetic_interviews - 1)

        with pytest.raises(HTTPException) as error:
            start(session, setup, user, Scope.DEMO, 2)

        assert error.value.status_code == 429
        assert "24 hours" in error.value.detail

    def test_the_daily_limit_can_be_filled_exactly(self, session, setup):
        user = uuid.uuid4()
        fill(session, setup, user, LIMITS.daily_synthetic_interviews - 1)

        start(session, setup, user, Scope.DEMO, 1)

    def test_yesterday_s_runs_do_not_count_today(self, session, setup):
        user = uuid.uuid4()
        fill(
            session,
            setup,
            user,
            LIMITS.daily_synthetic_interviews,
            age=DEMO_DAILY_WINDOW + timedelta(minutes=1),
        )

        start(session, setup, user, Scope.DEMO, 1)

    def test_failed_runs_count_towards_the_budget(self, session, setup):
        user = uuid.uuid4()
        fill(
            session,
            setup,
            user,
            LIMITS.daily_synthetic_interviews,
            status=TestRunStatus.FAILED,
        )

        with pytest.raises(HTTPException) as error:
            start(session, setup, user, Scope.DEMO, 1)

        assert error.value.status_code == 429

    def test_the_lifetime_limit_outlasts_the_day(self, session, setup):
        user = uuid.uuid4()
        fill(
            session,
            setup,
            user,
            LIMITS.lifetime_synthetic_interviews,
            age=timedelta(days=30),
        )

        with pytest.raises(HTTPException) as error:
            start(session, setup, user, Scope.DEMO, 1)

        assert error.value.status_code == 429
        assert "in total" in error.value.detail

    def test_other_users_have_no_daily_or_lifetime_limit(self, session, setup):
        user = uuid.uuid4()
        fill(session, setup, user, LIMITS.lifetime_synthetic_interviews)

        start(session, setup, user, Scope.USER, 1)
