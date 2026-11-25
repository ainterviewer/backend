"""A delete takes the deleted rows' files with it: projects', interviews'
(respondents' recordings and images) and experiments'. And
`orphaned_storage` finds what earlier deletes left behind.

Driven through the routes, with media storage in a temporary directory.
"""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ainterviewer.settings import settings as lib_settings
from app.auth import AuthToken
from app.db.crud import InterviewDataBase
from app.db.repositories.project import render_default_guide
from app.db.tables import (
    Base,
    CollaboratorTable,
    ExperimentTable,
    InterviewTable,
    ProjectFolderTable,
    UserTable,
)
from app.db.triggers import register_trigger_functions
from app.dependencies import get_db
from app.main import app
from app.services.storage_cleanup import orphaned_storage
from app.types import CollaboratorRole, InterviewType, Scope
from tests.helpers import ask_first_question


class World:
    """Two users' projects, each with an interview, and media for all."""

    def __init__(self, engine, storage):
        self.engine = engine
        self.storage = storage

    def dir(self, kind: str, entity_id) -> Path:
        return self.storage / kind / str(entity_id)

    def add_media(self, kind: str, entity_id) -> None:
        path = self.dir(kind, entity_id)
        path.mkdir(parents=True)
        (path / "file").write_bytes(b"x")

    def session(self) -> Session:
        return Session(self.engine)


@pytest.fixture
def world(tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    monkeypatch.setattr(lib_settings.storage, "base_path", storage)
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    register_trigger_functions(engine)
    Base.metadata.create_all(engine)
    w = World(engine, storage)

    with w.session() as session:
        database = InterviewDataBase(session)
        for name in ("ada", "bob"):
            user = UserTable(email=f"{name}@example.com", password="x", first_name=name)
            folder = ProjectFolderTable(title=name)
            session.add_all([user, folder])
            session.flush()
            session.add(
                CollaboratorTable(
                    folder_id=folder.id, user_id=user.id, role=CollaboratorRole.ADMIN
                )
            )
            session.commit()
            project_id = database.projects.create_project(
                folder_id=folder.id, title=name, owner_id=user.id, default_language="EN"
            )
            ask_first_question(database, project_id)
            interview = InterviewTable(
                project_id=project_id,
                interview_guide=render_default_guide(session, project_id),
                type=InterviewType.MANUAL_TEST,
            )
            experiment = ExperimentTable(title=name, user_id=user.id)
            session.add_all([interview, experiment])
            session.commit()
            setattr(w, name, user.id)
            setattr(w, f"{name}_folder", folder.id)
            setattr(w, f"{name}_project", project_id)
            setattr(w, f"{name}_interview", interview.id)
            setattr(w, f"{name}_experiment", experiment.id)
            w.add_media("projects", project_id)
            w.add_media("interviews", interview.id)
            w.add_media("experiments", experiment.id)

    def _db():
        with w.session() as session:
            yield InterviewDataBase(session)

    app.dependency_overrides[get_db] = _db
    yield w
    app.dependency_overrides.clear()


def client_as(user_id, scope=Scope.USER) -> TestClient:
    client = TestClient(app)
    client.cookies.set("access_token", AuthToken(user_id=user_id, scope=scope).encode())
    return client


def test_deleting_a_project_removes_its_and_its_interviews_files(world):
    response = client_as(world.ada).delete(f"/api/projects/{world.ada_project}")

    assert response.status_code == 200, response.text
    assert not world.dir("projects", world.ada_project).exists()
    assert not world.dir("interviews", world.ada_interview).exists()
    assert world.dir("interviews", world.bob_interview).exists()


def test_deleting_interviews_removes_only_those_deleted(world):
    """Bob's interview id sent to Ada's project is ignored by the delete, so
    its files stay."""
    response = client_as(world.ada).request(
        "DELETE",
        f"/api/projects/{world.ada_project}/interviews",
        json={"interview_ids": [str(world.ada_interview), str(world.bob_interview)]},
    )

    assert response.status_code == 200, response.text
    assert not world.dir("interviews", world.ada_interview).exists()
    assert world.dir("interviews", world.bob_interview).exists()
    assert world.dir("projects", world.ada_project).exists()


def test_deleting_a_folder_removes_its_projects_files(world):
    response = client_as(world.ada).delete(f"/api/folders/{world.ada_folder}")

    assert response.status_code == 200, response.text
    assert not world.dir("projects", world.ada_project).exists()
    assert not world.dir("interviews", world.ada_interview).exists()
    assert world.dir("projects", world.bob_project).exists()


def test_deleting_an_account_removes_its_projects_and_experiments_files(world):
    response = client_as(uuid.uuid4(), Scope.ADMIN).delete(
        "/api/admin/users", params={"user_id": str(world.ada)}
    )

    assert response.status_code == 200, response.text
    assert not world.dir("projects", world.ada_project).exists()
    assert not world.dir("interviews", world.ada_interview).exists()
    assert not world.dir("experiments", world.ada_experiment).exists()
    assert world.dir("experiments", world.bob_experiment).exists()


def test_deleting_an_experiment_removes_its_files_only_when_it_was_yours(world):
    ada = client_as(world.ada)

    ada.delete(f"/api/experiments/{world.bob_experiment}")
    assert world.dir("experiments", world.bob_experiment).exists()

    ada.delete(f"/api/experiments/{world.ada_experiment}")
    assert not world.dir("experiments", world.ada_experiment).exists()


def test_orphaned_storage_finds_what_has_no_row(world):
    stray_interview = uuid.uuid4()
    world.add_media("interviews", stray_interview)
    world.add_media("projects", uuid.uuid4())
    (world.storage / "projects" / "not-an-id").mkdir()

    with world.session() as session:
        orphans = orphaned_storage(session)

    assert orphans.interview_ids == (stray_interview,)
    assert len(orphans.project_ids) == 1
    assert orphans.experiment_ids == ()

    orphans.remove()
    assert not world.dir("interviews", stray_interview).exists()
    assert world.dir("interviews", world.ada_interview).exists()
    assert (world.storage / "projects" / "not-an-id").exists()
