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

import pytest
from fastapi import BackgroundTasks, HTTPException, Request
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ainterviewer.types import TestType
from ainterviewer.utils import now
from app.api.admin.users import get_demo_limit_defaults, get_user_demo_budget
from app.api.dashboard.synthesize import DEMO_DAILY_WINDOW, run_synthetic_test
from app.api.me import get_my_demo_budget
from app.auth import AuthToken
from app.db.crud import InterviewDataBase
from app.db.repositories.test import TestRepository
from app.db.tables import Base, TestRunTable, TestSetupTable, UserTable
from app.schemas.testing import SynthesizeRequest
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
        fixed_answers={str(uuid.uuid4()): "Yes."},
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
    # The endpoint reads only the access_token cookie off the request, to hand
    # to the background job, which these tests never run.
    request = Request({"type": "http", "headers": [(b"cookie", b"access_token=token")]})
    endpoint = run_synthetic_test(
        request=request,
        project_id=PROJECT,
        test_id=setup.id,
        request_data=SynthesizeRequest(n_interviews=n, answering_model="gpt-oss-120b"),
        background_tasks=BackgroundTasks(),
        db=InterviewDataBase(session),
        jwt=AuthToken(user_id=user, scope=scope),
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


def demo_user(session, **overrides) -> uuid.UUID:
    user = UserTable(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@example.com",
        password="x",
        first_name="Demo",
        scope=Scope.DEMO,
        **overrides,
    )
    session.add(user)
    session.commit()
    return user.id


class TestPerUserOverrides:
    def test_a_user_without_overrides_gets_the_defaults(self, session, setup):
        user = demo_user(session)

        assert InterviewDataBase(session).users.get_demo_limits(user) == LIMITS

    def test_a_raised_lifetime_limit_lets_the_user_go_past_the_default(
        self, session, setup
    ):
        user = demo_user(
            session, demo_lifetime_limit=LIMITS.lifetime_synthetic_interviews + 5
        )
        fill(
            session,
            setup,
            user,
            LIMITS.lifetime_synthetic_interviews,
            age=timedelta(days=30),
        )

        start(session, setup, user, Scope.DEMO, 1)

    def test_a_lowered_concurrent_limit_is_enforced(self, session, setup):
        user = demo_user(session, demo_concurrent_limit=2)

        with pytest.raises(HTTPException) as error:
            start(session, setup, user, Scope.DEMO, 3)

        assert error.value.status_code == 429
        assert "at most 2 synthetic interviews at a time" in error.value.detail

    def test_overrides_are_independent(self, session, setup):
        user = demo_user(session, demo_daily_limit=3)

        limits = InterviewDataBase(session).users.get_demo_limits(user)

        assert limits.daily_synthetic_interviews == 3
        assert (
            limits.max_concurrent_synthetic_interviews
            == LIMITS.max_concurrent_synthetic_interviews
        )
        assert (
            limits.lifetime_synthetic_interviews == LIMITS.lifetime_synthetic_interviews
        )


def budget(session, user, scope=Scope.DEMO):
    endpoint = get_my_demo_budget(
        db=InterviewDataBase(session), jwt=AuthToken(user_id=user, scope=scope)
    )
    return asyncio.run(endpoint)


class TestTheRemainingBudget:
    def test_it_reports_limits_usage_and_what_is_left(self, session, setup):
        user = demo_user(session, demo_daily_limit=20)
        run(session, setup, user, 3)
        run(session, setup, user, 4, status=TestRunStatus.COMPLETED)
        run(
            session,
            setup,
            user,
            5,
            status=TestRunStatus.COMPLETED,
            age=timedelta(days=2),
        )

        result = budget(session, user)

        assert result is not None
        assert result.concurrent.used == 3
        assert result.daily.limit == 20
        assert result.daily.used == 7
        assert result.daily.remaining == 13
        assert result.lifetime.used == 12

    def test_remaining_never_goes_negative(self, session, setup):
        user = demo_user(session, demo_lifetime_limit=1)
        run(session, setup, user, 5, status=TestRunStatus.COMPLETED)

        result = budget(session, user)

        assert result is not None
        assert result.lifetime.remaining == 0

    def test_other_scopes_have_no_budget(self, session, setup):
        assert budget(session, uuid.uuid4(), scope=Scope.USER) is None


class TestTheAdminView:
    def test_the_defaults_are_the_configured_limits(self):
        admin = AuthToken(user_id=uuid.uuid4(), scope=Scope.ADMIN)

        defaults = asyncio.run(get_demo_limit_defaults(jwt=admin))

        assert defaults.concurrent == LIMITS.max_concurrent_synthetic_interviews
        assert defaults.daily == LIMITS.daily_synthetic_interviews
        assert defaults.lifetime == LIMITS.lifetime_synthetic_interviews

    def test_an_admin_sees_a_user_s_budget_with_overrides(self, session, setup):
        user = demo_user(session, demo_lifetime_limit=7)
        run(session, setup, user, 2, status=TestRunStatus.COMPLETED)
        admin = AuthToken(user_id=uuid.uuid4(), scope=Scope.ADMIN)

        result = asyncio.run(
            get_user_demo_budget(user_id=user, db=InterviewDataBase(session), jwt=admin)
        )

        assert result.lifetime.limit == 7
        assert result.lifetime.used == 2
