"""Single-participant routes only reach participants of the project in their path.

The role check on these routes is against `{project_id}`, but the repository
looked the participant link up by id alone -- so a viewer of one project could
read another project's participant by naming its id, an editor change it (and
with it every project of its folder, which shares the profile), and an admin
delete it.
"""

import asyncio
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from app.api.dashboard.participants import (
    delete_participant,
    get_participant,
    update_participant,
)
from app.auth import AuthToken
from app.db.crud import InterviewDataBase
from app.db.tables import (
    Base,
    ParticipantTable,
    ProjectFolderTable,
    ProjectParticipantTable,
    ProjectTable,
    UserTable,
)
from app.schemas.participants import ParticipantUpdate
from app.types import Scope

# The routes are called directly, so their role dependencies never run; any
# token satisfies the signature.
TOKEN = AuthToken(user_id=uuid.uuid4(), scope=Scope.USER)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        user = UserTable(email="a@example.com", password="x", first_name="A")
        folder = ProjectFolderTable(title="f")
        project = ProjectTable(title="p", folder=folder, owner=user)
        other = ProjectTable(title="q", folder=folder, owner=user)
        person = ParticipantTable(folder=folder, pid="p1", name="Alice")
        link = ProjectParticipantTable(project=other, participant=person)
        session.add_all([user, folder, project, other, person, link])
        session.commit()
        yield InterviewDataBase(session), project.id, link.id


def test_another_projects_participant_cannot_be_reached_by_id(db):
    database, project_id, other = db
    with pytest.raises(NoResultFound):
        database.participants.get_participant(project_id, other)
    with pytest.raises(NoResultFound):
        database.participants.update_participant(
            project_id, other, ParticipantUpdate(name="Mallory")
        )
    with pytest.raises(NoResultFound):
        database.participants.remove_participant(project_id, other)

    link = database.session.get(ProjectParticipantTable, other)
    assert link is not None
    assert link.participant.name == "Alice"


@pytest.mark.parametrize(
    "call",
    [
        lambda db, project, link: get_participant(project, link, db, TOKEN, TOKEN),
        lambda db, project, link: update_participant(
            project, link, ParticipantUpdate(name="Mallory"), db, TOKEN, TOKEN
        ),
        lambda db, project, link: delete_participant(project, link, db, TOKEN, TOKEN),
    ],
    ids=["get", "update", "delete"],
)
@pytest.mark.parametrize("target", ["other_project", "missing"])
def test_routes_answer_404_for_a_participant_outside_the_project(db, call, target):
    database, project_id, other = db
    link = other if target == "other_project" else uuid.uuid4()
    with pytest.raises(HTTPException) as raised:
        asyncio.run(call(database, project_id, link))
    assert raised.value.status_code == 404
