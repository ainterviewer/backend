"""Experiments assign each respondent a project on the backend.

The old monorepo drew the project in its own `/interview/redirect` route; the
move to SvelteKit dropped that route, so experiment links 404'd, and the
experiment id the interview page passed on was never stored either. The draw
now happens in `POST /experiments/{id}/enter` and is kept in
`experiment_assignment`, and an interview takes its experiment from the
respondent's assignment rather than from the link, so a respondent can
neither pick a project nor tag an interview with an experiment by editing a
URL or a cookie.
"""

import random
from collections import Counter
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ainterviewer.types import InterviewStatus
from app.db.crud import InterviewDataBase
from app.db.tables import (
    Base,
    ExperimentAssignmentTable,
    ExperimentTable,
    InterviewTable,
    ProjectFolderTable,
    ProjectTable,
    UserTable,
)
from app.dependencies import get_db, register_trigger_functions
from app.main import app
from app.schemas.projects import ExperimentCreate, ExperimentProjectCreate
from app.types import ExperimentAllocation, ProjectStatus


@pytest.fixture
def engine():
    # One connection, shared with the TestClient's thread: every new
    # connection to `sqlite://` would be a separate, empty database.
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _enforce(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    register_trigger_functions(engine)
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def setup(engine):
    with Session(engine) as session:
        user = UserTable(email="a@example.com", password="x", first_name="A")
        folder = ProjectFolderTable(title="f")
        session.add_all([user, folder])
        session.commit()
        database = InterviewDataBase(session)

        def project(title, publish=True):
            project_id = database.projects.create_project(
                folder_id=folder.id,
                title=title,
                owner_id=user.id,
                default_language="EN",
            )
            if publish:
                database.versions.publish(project_id, user.id, None)
            return project_id

        projects = {
            "a": project("a"),
            "b": project("b"),
            "unpublished": project("unpublished", publish=False),
        }
        yield database, user.id, projects


def experiment(database, user_id, arms, allocation=ExperimentAllocation.RANDOM):
    """`arms` maps a project to its weight, or to (weight, target)."""
    projects = []
    for project_id, arm in arms.items():
        weight, target = arm if isinstance(arm, tuple) else (arm, None)
        projects.append(
            ExperimentProjectCreate(project_id=project_id, weight=weight, target=target)
        )
    return database.tests.create_experiment(
        ExperimentCreate(title="E", allocation=allocation, projects=projects),
        user_id=user_id,
    ).id


def newcomers(database, experiment_id, n=400, seed=0):
    """Projects drawn for `n` respondents entering for the first time."""
    rng = random.Random(seed)
    return Counter(
        database.tests.enter_experiment(
            experiment_id, f"browser-{i}", rng=rng
        ).project_id
        for i in range(n)
    )


def complete(database, experiment_id, project_id, n):
    for _ in range(n):
        database.session.add(
            InterviewTable(
                project_id=project_id,
                experiment_id=experiment_id,
                status=InterviewStatus.COMPLETED,
                interview_guide={},
                type="distributed",
            )
        )
    database.session.commit()


# ---------------------------------------------------------------- allocation


def test_projects_are_drawn_by_weight(setup):
    database, user_id, projects = setup
    experiment_id = experiment(database, user_id, {projects["a"]: 3, projects["b"]: 1})
    counts = newcomers(database, experiment_id)
    assert set(counts) == {projects["a"], projects["b"]}
    assert counts[projects["a"]] > 2 * counts[projects["b"]]


def test_unset_weight_counts_as_one_and_zero_leaves_a_project_out(setup):
    database, user_id, projects = setup
    experiment_id = experiment(
        database, user_id, {projects["a"]: None, projects["b"]: 0}
    )
    assert set(newcomers(database, experiment_id, n=50)) == {projects["a"]}


def test_inactive_and_unpublished_projects_are_skipped(setup):
    database, user_id, projects = setup
    experiment_id = experiment(
        database,
        user_id,
        {projects["a"]: 1, projects["b"]: 1, projects["unpublished"]: 1},
    )
    database.session.execute(
        update(ProjectTable)
        .where(ProjectTable.id == projects["b"])
        .values(status=ProjectStatus.INACTIVE)
    )
    assert set(newcomers(database, experiment_id, n=50)) == {projects["a"]}


def test_balanced_allocation_follows_the_weights_exactly(setup):
    database, user_id, projects = setup
    experiment_id = experiment(
        database,
        user_id,
        {projects["a"]: 2, projects["b"]: 1},
        allocation=ExperimentAllocation.BALANCED,
    )
    counts = newcomers(database, experiment_id, n=30)
    assert counts == Counter({projects["a"]: 20, projects["b"]: 10})


def test_balancing_on_completions_counts_completed_interviews(setup):
    database, user_id, projects = setup
    experiment_id = experiment(
        database,
        user_id,
        {projects["a"]: 1, projects["b"]: 1},
        allocation=ExperimentAllocation.BALANCED_COMPLETED,
    )
    complete(database, experiment_id, projects["a"], 3)
    # b is three completions behind: the next three newcomers all go there.
    assert newcomers(database, experiment_id, n=3) == Counter({projects["b"]: 3})


def test_a_full_target_closes_a_project_to_newcomers_only(setup):
    database, user_id, projects = setup
    experiment_id = experiment(
        database, user_id, {projects["a"]: (1, 2), projects["b"]: (1, None)}
    )
    early = database.tests.enter_experiment(
        experiment_id, "early", rng=random.Random(1)
    )
    complete(database, experiment_id, projects["a"], 2)

    assert set(newcomers(database, experiment_id, n=50, seed=2)) == {projects["b"]}
    # Whoever was already given a project keeps it, full or not.
    assert database.tests.enter_experiment(experiment_id, "early").project_id == (
        early.project_id
    )


def test_every_target_reached_closes_the_experiment(setup):
    database, user_id, projects = setup
    experiment_id = experiment(database, user_id, {projects["a"]: (1, 1)})
    complete(database, experiment_id, projects["a"], 1)
    assert database.tests.enter_experiment(experiment_id, "late") is None


def test_inactive_or_unknown_experiments_take_nobody(setup):
    database, user_id, projects = setup
    experiment_id = experiment(database, user_id, {projects["a"]: 1})
    database.session.execute(
        update(ExperimentTable)
        .where(ExperimentTable.id == experiment_id)
        .values(status=ProjectStatus.INACTIVE)
    )
    assert database.tests.enter_experiment(experiment_id, "x") is None
    with pytest.raises(NoResultFound):
        database.tests.enter_experiment(uuid4(), "x")


# --------------------------------------------------------------- respondents


def test_a_browser_keeps_its_project(setup):
    database, user_id, projects = setup
    experiment_id = experiment(database, user_id, {projects["a"]: 1, projects["b"]: 1})
    first = database.tests.enter_experiment(experiment_id, "same", rng=random.Random(0))
    for seed in range(20):
        again = database.tests.enter_experiment(
            experiment_id, "same", rng=random.Random(seed)
        )
        assert again.id == first.id
        assert again.project_id == first.project_id


def test_a_known_participant_keeps_their_project_in_any_browser(setup):
    database, user_id, projects = setup
    experiment_id = experiment(database, user_id, {projects["a"]: 1, projects["b"]: 1})
    first = database.tests.enter_experiment(experiment_id, "laptop", pid="p1")
    for seed in range(20):
        assert (
            database.tests.enter_experiment(
                experiment_id, f"phone-{seed}", pid="p1", rng=random.Random(seed)
            ).project_id
            == first.project_id
        )


def test_a_project_closing_reassigns_its_respondents(setup):
    database, user_id, projects = setup
    experiment_id = experiment(database, user_id, {projects["a"]: 1, projects["b"]: 0})
    assert (
        database.tests.enter_experiment(experiment_id, "r").project_id == projects["a"]
    )
    database.session.execute(
        update(ProjectTable)
        .where(ProjectTable.id == projects["a"])
        .values(status=ProjectStatus.INACTIVE)
    )
    # b takes no newcomers (weight 0), so with a closed nobody can be placed.
    assert database.tests.enter_experiment(experiment_id, "r") is None


def test_deleting_an_experiment_can_reopen_its_projects(setup):
    database, user_id, projects = setup
    for project_id in projects.values():
        database.projects.set_experiment_only(project_id, True)
    doomed = experiment(database, user_id, {projects["a"]: 1, projects["b"]: 1})
    experiment(database, user_id, {projects["b"]: 1})

    database.tests.delete_experiment(doomed, user_id, clear_experiment_only=True)
    only = dict(
        database.session.execute(
            select(ProjectTable.id, ProjectTable.experiment_only)
        ).all()
    )
    # b is still in the other experiment, and `unpublished` was never in this one.
    assert only == {
        projects["a"]: False,
        projects["b"]: True,
        projects["unpublished"]: True,
    }


# -------------------------------------------------------------------- HTTP


@pytest.fixture
def client(engine, setup):
    database, user_id, projects = setup

    def _db():
        with Session(engine) as session:
            yield InterviewDataBase(session)

    app.dependency_overrides[get_db] = _db
    # https: the respondent and interview cookies are Secure, and httpx sends
    # them back over https only.
    yield TestClient(app, base_url="https://testserver"), database, user_id, projects
    app.dependency_overrides.clear()


def enter(client, experiment_id, **body):
    return client.post(f"/api/experiments/{experiment_id}/enter", json=body)


def start_interview(client, project_id, **body):
    return client.post(f"/api/projects/{project_id}/EN/interviews", json=body)


def assignment_of(database, interview_id):
    database.session.expire_all()
    return database.session.execute(
        select(ExperimentAssignmentTable).where(
            ExperimentAssignmentTable.interview_id == interview_id
        )
    ).scalar_one_or_none()


def interview_experiment(database, interview_id):
    database.session.expire_all()
    return database.session.get(InterviewTable, interview_id).experiment_id


def interview_id_of(client):
    from app.auth import InterviewToken

    return InterviewToken.decode(client.cookies["interview_token"]).interview_id


def test_entering_gives_the_browser_a_respondent_cookie_and_a_project(client):
    client, database, user_id, projects = client
    experiment_id = experiment(database, user_id, {projects["a"]: 1, projects["b"]: 1})

    first = enter(client, experiment_id)
    assert first.status_code == 200
    assert "respondent" in first.cookies
    project_id = first.json()["project_id"]
    for _ in range(10):
        again = enter(client, experiment_id)
        assert again.json()["project_id"] == project_id
        assert "respondent" not in again.cookies  # set once, not extended


def test_entering_an_unknown_or_closed_experiment(client):
    client, database, user_id, projects = client
    closed = experiment(database, user_id, {projects["unpublished"]: 1})
    assert enter(client, uuid4()).status_code == 404
    assert enter(client, closed).status_code == 409


def test_an_interview_takes_its_experiment_from_the_assignment(client):
    client, database, user_id, projects = client
    experiment_id = experiment(database, user_id, {projects["a"]: 1})
    enter(client, experiment_id)

    assert start_interview(client, projects["a"]).status_code == 200
    interview_id = interview_id_of(client)
    assert interview_experiment(database, interview_id) == experiment_id
    assert assignment_of(database, interview_id) is not None


def test_another_project_of_the_experiment_is_not_tagged(client):
    """Being assigned a is no claim on b: the respondent cannot move arms."""
    client, database, user_id, projects = client
    experiment_id = experiment(database, user_id, {projects["a"]: 1, projects["b"]: 0})
    enter(client, experiment_id)

    assert start_interview(client, projects["b"]).status_code == 200
    assert interview_experiment(database, interview_id_of(client)) is None


def test_an_experiment_only_project_refuses_respondents_it_was_not_given(client):
    client, database, user_id, projects = client
    database.projects.set_experiment_only(projects["b"], True)
    experiment_id = experiment(database, user_id, {projects["a"]: 1, projects["b"]: 0})
    enter(client, experiment_id)

    refused = start_interview(client, projects["b"])
    assert refused.status_code == 400
    assert refused.json()["code"] == "invalid_link_params"
    # Test interviews are not respondents.
    assert (
        start_interview(client, projects["b"], interview_type="manual_test").status_code
        != 400
    )


def test_an_experiment_only_project_takes_its_own_respondents(client):
    client, database, user_id, projects = client
    database.projects.set_experiment_only(projects["a"], True)
    experiment_id = experiment(database, user_id, {projects["a"]: 1})
    enter(client, experiment_id)
    assert start_interview(client, projects["a"]).status_code == 200


def test_entering_again_resumes_the_assignments_interview(client):
    """A project in two experiments gets an interview from each, and entering
    one again goes back to that one's interview, not the last one started."""
    client, database, user_id, projects = client
    first = experiment(database, user_id, {projects["a"]: 1})
    second = experiment(database, user_id, {projects["a"]: 1})

    enter(client, first)
    start_interview(client, projects["a"])
    first_interview = interview_id_of(client)

    entered = enter(client, second)
    # The first experiment's interview is not this one's to resume.
    assert entered.cookies.get("interview_token") is None
    assert "interview_token" not in client.cookies
    start_interview(client, projects["a"])
    second_interview = interview_id_of(client)
    assert second_interview != first_interview
    assert interview_experiment(database, second_interview) == second

    enter(client, first)
    assert interview_id_of(client) == first_interview


def test_a_known_participants_interview_resumes_only_in_its_own_browser(client):
    client, database, user_id, projects = client
    experiment_id = experiment(database, user_id, {projects["a"]: 1})
    enter(client, experiment_id, pid="p1")
    start_interview(client, projects["a"], pid="p1")

    other = TestClient(app, base_url="https://testserver")
    entered = enter(other, experiment_id, pid="p1")
    assert entered.json()["project_id"] == str(projects["a"])
    assert "interview_token" not in other.cookies
