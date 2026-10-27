"""An experiment's settings can be edited after it is created.

The dashboard offered an Edit action with nothing behind it. `PUT
/experiments/{id}` replaces the title, allocation and projects, keeping the
links of the projects it keeps so their counts carry on, and refusing to
remove a project respondents were already given.
"""

import asyncio
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.dashboard.experiments import create_experiment, update_experiment
from app.auth import AuthToken
from app.db.crud import InterviewDataBase
from app.db.tables import (
    Base,
    CollaboratorTable,
    ExperimentAssignmentTable,
    ProjectFolderTable,
    ProjectTable,
    UserTable,
)
from app.schemas.projects import (
    ExperimentCreate,
    ExperimentProjectCreate,
    ExperimentUpdate,
)
from app.types import CollaboratorRole, ExperimentAllocation, Scope


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        owner = UserTable(email="o@example.com", password="x", first_name="O")
        user = UserTable(email="u@example.com", password="x", first_name="U")
        session.add_all([owner, user])
        session.flush()

        projects = {}
        for name, role in [
            ("a", CollaboratorRole.EDITOR),
            ("b", CollaboratorRole.EDITOR),
            ("viewable", CollaboratorRole.VIEWER),
        ]:
            folder = ProjectFolderTable(title=name)
            projects[name] = ProjectTable(title=name, folder=folder, owner=owner)
            session.add_all([folder, projects[name]])
            session.flush()
            session.add(
                CollaboratorTable(folder_id=folder.id, user_id=user.id, role=role)
            )
        session.commit()
        yield (
            InterviewDataBase(session),
            AuthToken(user_id=user.id, scope=Scope.USER),
            {name: project.id for name, project in projects.items()},
        )


def _create(db, jwt, arms):
    request = ExperimentCreate(
        title="E",
        projects=[ExperimentProjectCreate(project_id=p, **arm) for p, arm in arms],
    )
    return asyncio.run(create_experiment(request, db, jwt))


def _update(db, jwt, experiment_id, arms, title="E", allocation="random"):
    request = ExperimentUpdate(
        title=title,
        allocation=allocation,
        projects=[ExperimentProjectCreate(project_id=p, **arm) for p, arm in arms],
    )
    return asyncio.run(update_experiment(experiment_id, request, db, jwt))


def test_settings_are_replaced_and_kept_links_carry_on(db):
    database, jwt, projects = db
    created = _create(database, jwt, [(projects["a"], {"weight": 1})])

    updated = _update(
        database,
        jwt,
        created.id,
        [(projects["a"], {"weight": 2, "target": 10}), (projects["b"], {})],
        title="Renamed",
        allocation="balanced",
    )

    assert updated.title == "Renamed"
    assert updated.allocation == ExperimentAllocation.BALANCED
    arms = {arm.project_id: arm for arm in updated.projects}
    assert arms.keys() == {projects["a"], projects["b"]}
    assert (arms[projects["a"]].weight, arms[projects["a"]].target) == (2, 10)
    # The same link, not a new one.
    assert arms[projects["a"]].id == created.projects[0].id


def test_a_project_without_respondents_can_be_removed(db):
    """Its `experiment_only` is cleared, as nothing could assign to it."""
    database, jwt, projects = db
    created = _create(
        database,
        jwt,
        [(projects["a"], {}), (projects["b"], {"experiment_only": True})],
    )

    updated = _update(database, jwt, created.id, [(projects["a"], {})])

    assert [arm.project_id for arm in updated.projects] == [projects["a"]]
    assert database.session.get(ProjectTable, projects["b"]).experiment_only is False


def test_a_project_another_experiment_uses_stays_experiment_only(db):
    database, jwt, projects = db
    arms = [(projects["a"], {}), (projects["b"], {"experiment_only": True})]
    created = _create(database, jwt, arms)
    _create(database, jwt, arms)

    _update(database, jwt, created.id, [(projects["a"], {})])

    assert database.session.get(ProjectTable, projects["b"]).experiment_only is True


def test_a_project_respondents_were_given_cannot_be_removed(db):
    database, jwt, projects = db
    created = _create(database, jwt, [(projects["a"], {}), (projects["b"], {})])
    database.session.add(
        ExperimentAssignmentTable(
            experiment_id=created.id, project_id=projects["b"], respondent_hash="h"
        )
    )
    database.session.commit()

    with pytest.raises(HTTPException) as raised:
        _update(database, jwt, created.id, [(projects["a"], {})])
    assert raised.value.status_code == 409

    # Closing it to new respondents is still allowed.
    updated = _update(
        database, jwt, created.id, [(projects["a"], {}), (projects["b"], {"weight": 0})]
    )
    assert {arm.project_id: arm.weight for arm in updated.projects}[projects["b"]] == 0


def test_adding_a_project_takes_the_editor_role(db):
    database, jwt, projects = db
    created = _create(database, jwt, [(projects["a"], {})])

    with pytest.raises(HTTPException) as raised:
        _update(
            database, jwt, created.id, [(projects["a"], {}), (projects["viewable"], {})]
        )
    assert raised.value.status_code == 403
    assert [
        arm.project_id
        for arm in database.tests.get_experiments(jwt.user_id)[0].projects
    ] == [projects["a"]]


def test_a_kept_project_needs_no_role_unless_experiment_only_changes(db):
    """Losing the editor role on a linked project leaves its weight editable,
    but not the project's own `experiment_only` setting."""
    database, jwt, projects = db
    created = _create(database, jwt, [(projects["a"], {})])
    database.session.query(CollaboratorTable).filter_by(user_id=jwt.user_id).update(
        {"role": CollaboratorRole.VIEWER}
    )
    database.session.commit()

    assert _update(database, jwt, created.id, [(projects["a"], {"weight": 3})])
    with pytest.raises(HTTPException) as raised:
        _update(database, jwt, created.id, [(projects["a"], {"experiment_only": True})])
    assert raised.value.status_code == 403


def test_someone_elses_experiment_is_not_found(db):
    database, jwt, projects = db
    created = _create(database, jwt, [(projects["a"], {})])
    stranger = AuthToken(user_id=uuid.uuid4(), scope=Scope.USER)

    for experiment_id in (created.id, uuid.uuid4()):
        with pytest.raises(HTTPException) as raised:
            _update(database, stranger, experiment_id, [(projects["a"], {})])
        assert raised.value.status_code == 404
