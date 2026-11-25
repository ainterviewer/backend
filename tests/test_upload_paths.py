"""A file name a client sends never becomes a path outside the storage
directory it is meant for.

The welcome video and guide images are stored under the name they arrive
with, and FastAPI hands an upload's name over exactly as sent, "../" and all.
Interview uploads keep only the extension, which has to be just that.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ainterviewer.settings import settings as lib_settings
from app.api.files import safe_suffix
from app.auth import AuthToken
from app.collab.parts import LIVE_PARTS
from app.db.crud import InterviewDataBase
from app.db.tables import Base, CollaboratorTable, ProjectFolderTable, UserTable
from app.db.triggers import register_trigger_functions
from app.dependencies import get_db
from app.main import app
from app.types import CollaboratorRole, ContentPart, Scope
from tests.helpers import ask_first_question


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(lib_settings.storage, "base_path", tmp_path / "storage")
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
                folder_id=folder.id, user_id=user.id, role=CollaboratorRole.EDITOR
            )
        )
        session.commit()
        database = InterviewDataBase(session)
        project_id = database.projects.create_project(
            folder_id=folder.id, title="p", owner_id=user.id, default_language="EN"
        )
        ask_first_question(database, project_id)
        user_id = user.id

    def _db():
        with Session(engine) as session:
            yield InterviewDataBase(session)

    app.dependency_overrides[get_db] = _db
    client = TestClient(app)
    client.cookies.set(
        "access_token", AuthToken(user_id=user_id, scope=Scope.USER).encode()
    )
    yield client, project_id, tmp_path
    app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "name",
    ["../../evil.mp4", "a/../../evil.mp4", "..\\..\\evil.mp4", "C:\\x\\evil.mp4"],
)
def test_a_welcome_video_keeps_only_the_file_name(setup, name):
    """A path in either style is dropped, not refused: the file lands among
    the project's videos under its bare name."""
    client, project_id, tmp_path = setup

    response = client.post(
        f"/api/projects/{project_id}/welcome/video",
        files={"video": (name, b"video", "video/mp4")},
    )

    assert response.status_code == 200, response.text
    assert response.json() == "evil.mp4"
    videos = tmp_path / "storage" / "projects" / str(project_id) / "videos"
    assert [p.relative_to(tmp_path) for p in tmp_path.rglob("*evil.mp4")] == [
        (videos / "evil.mp4").relative_to(tmp_path)
    ]


def test_a_welcome_video_must_be_a_video(setup):
    client, project_id, _ = setup

    response = client.post(
        f"/api/projects/{project_id}/welcome/video",
        files={"video": ("page.html", b"<script>", "text/html")},
    )

    assert response.status_code == 400


def test_a_welcome_video_is_stored_under_its_name(setup):
    client, project_id, tmp_path = setup

    response = client.post(
        f"/api/projects/{project_id}/welcome/video",
        files={"video": ("Welcome.mp4", b"video", "video/mp4")},
    )

    assert response.status_code == 200, response.text
    assert response.json() == "Welcome.mp4"
    stored = (
        tmp_path / "storage" / "projects" / str(project_id) / "videos" / "Welcome.mp4"
    )
    assert stored.read_bytes() == b"video"


def test_an_uploaded_guide_image_gets_a_name_the_guide_accepts(setup):
    """A client sending a Windows path keeps only the file name, which
    saving the guide then accepts."""
    client, project_id, _ = setup

    uploaded = client.post(
        f"/api/projects/{project_id}/image",
        files={"file": ("C:\\Users\\ada\\photo.png", b"png", "image/png")},
    )

    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json() == "photo.png"
    guide, etag = _guide_with_image(client, project_id, uploaded.json())
    guide["question_sections"][0]["questions"][0]["image"]["data"] = None
    saved = client.put(
        f"/api/projects/{project_id}/guide", json=guide, headers={"If-Match": etag}
    )
    assert saved.status_code == 200, saved.text


def _guide_with_image(client, project_id, name):
    loaded = client.get(f"/api/projects/{project_id}/guide")
    guide = loaded.json()
    guide["question_sections"][0]["questions"][0]["image"] = {
        "type": "image",
        "name": name,
        "data": "data:image/png;base64,iVBORw0KGgo=",
    }
    return guide, loaded.headers["ETag"]


@pytest.mark.parametrize("name", ["../../evil.png", "..\\evil.png", "a/evil.png"])
def test_a_guide_image_name_with_a_path_is_refused(setup, name):
    client, project_id, tmp_path = setup
    guide, etag = _guide_with_image(client, project_id, name)

    response = client.put(
        f"/api/projects/{project_id}/guide", json=guide, headers={"If-Match": etag}
    )

    assert response.status_code == 400
    assert "not a file name" in response.json()["detail"]
    assert not any(tmp_path.rglob("evil.png"))


def test_a_guide_image_with_a_bare_name_is_saved(setup):
    client, project_id, tmp_path = setup
    guide, etag = _guide_with_image(client, project_id, "photo.png")

    response = client.put(
        f"/api/projects/{project_id}/guide", json=guide, headers={"If-Match": etag}
    )

    assert response.status_code == 200, response.text
    assert (
        tmp_path / "storage" / "projects" / str(project_id) / "images" / "photo.png"
    ).exists()


def test_the_live_guide_refuses_an_image_name_with_a_path(setup):
    client, project_id, _ = setup
    guide, _ = _guide_with_image(client, project_id, "../../evil.png")

    validated, errors = LIVE_PARTS[ContentPart.GUIDE].validate(guide, "EN")

    assert validated is None
    assert [e.field for e in errors] == ["image"]


@pytest.mark.parametrize(
    ("filename", "suffix"),
    [
        ("photo.png", ".png"),
        ("clip.webm", ".webm"),
        ("no-extension", ""),
        (None, ""),
        ("a./../../x", ""),
        ("a.png/../../x", ""),
        ("evil.p/ng", ""),
        ("x.p ng", ""),
        ("..\\..\\x.wav", ".wav"),
    ],
)
def test_only_a_short_alphanumeric_extension_is_kept(filename, suffix):
    assert safe_suffix(filename) == suffix
