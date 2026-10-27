"""Experiments only reach projects, and QR codes, their creator may use.

An experiment's projects arrive in the request body, so no role dependency
covers them: `create_experiment` used to link any project id it was sent,
letting any logged-in user route an experiment's respondents into a project
they have no access to. The QR route took any experiment id and wrote a file
for it.
"""

import asyncio
import uuid

import pytest
from fastapi import HTTPException, Request
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api.dashboard.experiments import create_experiment, generate_experiment_qr
from app.auth import AuthToken
from app.db.crud import InterviewDataBase
from app.db.tables import (
    Base,
    CollaboratorTable,
    ExperimentTable,
    ProjectFolderTable,
    ProjectTable,
    UserTable,
)
from app.schemas.projects import ExperimentCreate, ExperimentProjectCreate
from app.types import CollaboratorRole, Scope


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
            ("editable", CollaboratorRole.EDITOR),
            ("viewable", CollaboratorRole.VIEWER),
            ("foreign", None),
        ]:
            folder = ProjectFolderTable(title=name)
            projects[name] = ProjectTable(title=name, folder=folder, owner=owner)
            session.add_all([folder, projects[name]])
            session.flush()
            if role is not None:
                session.add(
                    CollaboratorTable(folder_id=folder.id, user_id=user.id, role=role)
                )
        session.commit()
        yield (
            InterviewDataBase(session),
            user.id,
            {name: project.id for name, project in projects.items()},
        )


def _create(db, jwt, project_ids):
    request = ExperimentCreate(
        title="E",
        projects=[ExperimentProjectCreate(project_id=p) for p in project_ids],
    )
    return asyncio.run(create_experiment(request, db, jwt))


def _experiment_count(db):
    return len(db.session.execute(select(ExperimentTable.id)).all())


def test_an_editor_can_link_their_projects(db):
    database, user_id, projects = db
    experiment = _create(
        database, AuthToken(user_id=user_id, scope=Scope.USER), [projects["editable"]]
    )
    assert [p.project_id for p in experiment.projects] == [projects["editable"]]


@pytest.mark.parametrize(
    ("target", "status"),
    [("viewable", 403), ("foreign", 404), ("missing", 404)],
)
def test_a_project_the_user_cannot_edit_is_refused(db, target, status):
    database, user_id, projects = db
    # Alongside one they can edit, so a single bad id refuses the whole request.
    ids = [projects["editable"], projects.get(target, uuid.uuid4())]
    with pytest.raises(HTTPException) as raised:
        _create(database, AuthToken(user_id=user_id, scope=Scope.USER), ids)
    assert raised.value.status_code == status
    assert _experiment_count(database) == 0


def test_an_admin_links_any_project_but_not_a_missing_one(db):
    database, user_id, projects = db
    admin = AuthToken(user_id=user_id, scope=Scope.ADMIN)
    assert _create(database, admin, [projects["foreign"]])
    with pytest.raises(HTTPException) as raised:
        _create(database, admin, [uuid.uuid4()])
    assert raised.value.status_code == 404


def test_qr_code_is_refused_for_someone_elses_experiment(db):
    """Refused before the file path is built, so nothing is written either."""
    database, user_id, projects = db
    experiment = _create(
        database, AuthToken(user_id=user_id, scope=Scope.USER), [projects["editable"]]
    )
    stranger = AuthToken(user_id=uuid.uuid4(), scope=Scope.USER)

    for experiment_id in (experiment.id, uuid.uuid4()):
        with pytest.raises(HTTPException) as raised:
            asyncio.run(
                generate_experiment_qr(
                    Request({"type": "http"}), experiment_id, stranger, database
                )
            )
        assert raised.value.status_code == 404
