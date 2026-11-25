"""Guide images go out by name and the browser loads them: respondents
through their interview, viewers through the project.

Driven through the app with an in-memory database and media storage in a
temporary directory.
"""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ainterviewer.interview_guides import InterviewGuide
from ainterviewer.settings import settings as lib_settings
from ainterviewer.types import Interviewer
from app.auth import AuthToken, InterviewToken
from app.db.crud import InterviewDataBase
from app.db.tables import (
    Base,
    CollaboratorTable,
    InterviewTable,
    ProjectFolderTable,
    UserTable,
)
from app.db.triggers import register_trigger_functions
from app.dependencies import get_db
from app.main import app
from app.services import project_storage
from app.types import CollaboratorRole, InterviewType, Scope

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _guide(image_name: str) -> InterviewGuide:
    return InterviewGuide.model_validate(
        {
            "framing": "Framing",
            "question_sections": [
                {
                    "description": "Section",
                    "questions": [
                        {
                            "main_question": "What do you see?",
                            "image": {
                                "name": image_name,
                                "description": "A photo",
                                "alt": "A photo",
                            },
                        }
                    ],
                }
            ],
        }
    )


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(lib_settings.storage, "base_path", tmp_path)
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    register_trigger_functions(engine)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        user = UserTable(email="a@example.com", password="x", first_name="Ada")
        folder = ProjectFolderTable(title="f")
        session.add_all([user, folder])
        session.flush()
        session.add(
            CollaboratorTable(
                folder_id=folder.id, user_id=user.id, role=CollaboratorRole.VIEWER
            )
        )
        session.commit()
        project_id = InterviewDataBase(session).projects.create_project(
            folder_id=folder.id, title="p", owner_id=user.id, default_language="EN"
        )
        interview = InterviewTable(
            project_id=project_id,
            interview_guide=_guide("shown.png"),
            type=InterviewType.MANUAL_TEST,
        )
        session.add(interview)
        session.commit()
        ids = {"user": user.id, "project": project_id, "interview": interview.id}

    images = project_storage.image_path(ids["project"])
    for name in ("shown.png", "other.png", "page.html"):
        (images / name).write_bytes(PNG)
    (tmp_path / "secret.png").write_bytes(PNG)

    def _db():
        with Session(engine) as session:
            yield InterviewDataBase(session)

    app.dependency_overrides[get_db] = _db
    yield ids
    app.dependency_overrides.clear()


def _respondent(ids, project_id=None) -> TestClient:
    client = TestClient(app)
    token = InterviewToken(
        project_id=project_id or ids["project"],
        interview_id=ids["interview"],
        interviewer=Interviewer.AI,
    )
    client.cookies.set("interview_token", token.encode())
    return client


def _viewer(ids) -> TestClient:
    client = TestClient(app)
    client.cookies.set(
        "access_token", AuthToken(user_id=ids["user"], scope=Scope.USER).encode()
    )
    return client


def test_a_respondent_loads_an_image_of_their_interview(setup):
    response = _respondent(setup).get("/api/interview/images/shown.png")

    assert response.status_code == 200, response.text
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]


@pytest.mark.parametrize("name", ["other.png", "..%2Fsecret.png", "missing.png"])
def test_a_respondent_gets_only_their_interview_s_images(setup, name):
    """other.png is the project's, but not in this interview's guide."""
    response = _respondent(setup).get(f"/api/interview/images/{name}")

    assert response.status_code == 404


def test_an_interview_token_for_another_project_finds_no_interview(setup):
    response = _respondent(setup, project_id=uuid4()).get(
        "/api/interview/images/shown.png"
    )

    assert response.status_code == 404


def test_a_respondent_needs_their_interview_token(setup):
    response = TestClient(app).get("/api/interview/images/shown.png")

    assert response.status_code == 401


def test_a_viewer_loads_any_of_the_project_s_images(setup):
    viewer = _viewer(setup)

    for name in ("shown.png", "other.png"):
        response = viewer.get(f"/api/projects/{setup['project']}/images/{name}")
        assert response.status_code == 200, response.text
        assert response.content == PNG


@pytest.mark.parametrize("name", ["page.html", "..%2Fsecret.png", "missing.png"])
def test_only_image_files_in_the_project_s_images_are_served(setup, name):
    response = _viewer(setup).get(f"/api/projects/{setup['project']}/images/{name}")

    assert response.status_code == 404


def test_someone_outside_the_project_gets_no_images(setup):
    outsider = TestClient(app)
    outsider.cookies.set(
        "access_token", AuthToken(user_id=uuid4(), scope=Scope.USER).encode()
    )

    response = outsider.get(f"/api/projects/{setup['project']}/images/shown.png")

    assert response.status_code in {403, 404}
