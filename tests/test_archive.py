"""Archiving folders and projects, over HTTP.

Archiving only hides: the folder listing still returns archived folders and
projects, stamped, for the dashboard to show or leave out. It is for admins,
and it is not an edit, so it leaves `last_updated` -- which the dashboard sorts
by -- where it was.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.auth import AuthToken
from app.db.crud import InterviewDataBase
from app.db.tables import Base, CollaboratorTable, ProjectFolderTable, UserTable
from app.db.triggers import register_trigger_functions
from app.dependencies import get_db
from app.main import app
from app.types import CollaboratorRole, Scope


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

    register_trigger_functions(engine)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        admin = UserTable(email="a@example.com", password="x", first_name="Ada")
        editor = UserTable(email="e@example.com", password="x", first_name="Eve")
        folder = ProjectFolderTable(title="f")
        session.add_all([admin, editor, folder])
        session.flush()
        session.add_all(
            [
                CollaboratorTable(
                    folder_id=folder.id, user_id=admin.id, role=CollaboratorRole.ADMIN
                ),
                CollaboratorTable(
                    folder_id=folder.id,
                    user_id=editor.id,
                    role=CollaboratorRole.EDITOR,
                ),
            ]
        )
        session.commit()
        project_id = InterviewDataBase(session).projects.create_project(
            folder_id=folder.id, title="p", owner_id=admin.id, default_language="EN"
        )
        ids = admin.id, editor.id, folder.id

    def _db():
        with Session(engine) as session:
            yield InterviewDataBase(session)

    app.dependency_overrides[get_db] = _db
    admin_id, editor_id, folder_id = ids

    def cookies(user_id):
        return {"access_token": AuthToken(user_id=user_id, scope=Scope.USER).encode()}

    yield TestClient(app), folder_id, project_id, cookies(admin_id), cookies(editor_id)
    app.dependency_overrides.clear()


def _listing(client, cookies):
    (folder,) = client.get("/api/folders", cookies=cookies).json()
    return folder, folder["projects"][0]


def test_archiving_stamps_and_unarchiving_clears(setup):
    client, folder_id, project_id, admin, _ = setup
    folder, project = _listing(client, admin)
    assert folder["archived_at"] is None
    assert project["archived_at"] is None

    for url in (
        f"/api/folders/{folder_id}/archive",
        f"/api/projects/{project_id}/archive",
    ):
        response = client.patch(url, json={"archived": True}, cookies=admin)
        assert response.status_code == 200

    folder, project = _listing(client, admin)
    assert folder["archived_at"] is not None
    assert project["archived_at"] is not None

    for url in (
        f"/api/folders/{folder_id}/archive",
        f"/api/projects/{project_id}/archive",
    ):
        client.patch(url, json={"archived": False}, cookies=admin)

    folder, project = _listing(client, admin)
    assert folder["archived_at"] is None
    assert project["archived_at"] is None


def test_archiving_leaves_last_updated_alone(setup):
    client, _, project_id, admin, _ = setup
    _, project = _listing(client, admin)

    client.patch(
        f"/api/projects/{project_id}/archive", json={"archived": True}, cookies=admin
    )

    _, archived = _listing(client, admin)
    assert archived["last_updated"] == project["last_updated"]


def test_only_admins_may_archive(setup):
    client, folder_id, project_id, _, editor = setup
    for url in (
        f"/api/folders/{folder_id}/archive",
        f"/api/projects/{project_id}/archive",
    ):
        response = client.patch(url, json={"archived": True}, cookies=editor)
        assert response.status_code == 403
