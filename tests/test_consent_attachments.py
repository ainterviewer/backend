"""PDFs attached to the consent: what editors may upload, and which ones
respondents in each language are served.

Driven through the app with an in-memory database and the project storage
pointed at a temporary directory.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ainterviewer.settings import settings as lib_settings
from app.api.dashboard.consent_attachments import prune_consent_attachments
from app.auth import AuthToken
from app.db.crud import InterviewDataBase
from app.db.repositories.errors import NotPublishedError
from app.db.tables import Base, CollaboratorTable, ProjectFolderTable, UserTable
from app.db.triggers import register_trigger_functions
from app.dependencies import get_db
from app.main import app
from app.services import project_storage
from app.types import CollaboratorRole, Scope
from tests.helpers import ask_first_question

PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


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
    response = client.post(
        f"/api/projects/{project_id}/available_languages",
        json={"language": "DA", "translate": False},
    )
    assert response.status_code == 200, response.text
    yield client, project_id
    app.dependency_overrides.clear()


def _upload(client, project_id, language, *files):
    return client.post(
        f"/api/projects/{project_id}/{language}/consent-attachments",
        files=[
            ("files", (name, content, "application/pdf")) for name, content in files
        ],
    )


def _respondent(client, project_id, language):
    return client.get(
        f"/api/projects/{project_id}/guide/consent/{language}/attachments",
        params={"draft": True},
    )


def test_an_editor_uploads_lists_and_deletes_pdfs(setup):
    client, project_id = setup

    uploaded = _upload(
        client, project_id, "EN", ("b sheet.pdf", PDF), ("A info.pdf", PDF)
    )

    assert uploaded.status_code == 200, uploaded.text
    assert [a["label"] for a in uploaded.json()] == ["A info", "b sheet"]
    assert uploaded.json()[0]["size"] == len(PDF)

    deleted = client.delete(
        f"/api/projects/{project_id}/EN/consent-attachments/A info.pdf"
    )
    assert [a["filename"] for a in deleted.json()] == ["b sheet.pdf"]


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("notes.txt", b"hello"),
        ("page.pdf", b"<html><script>alert(1)</script></html>"),
    ],
)
def test_only_pdfs_are_accepted(setup, name, content):
    client, project_id = setup

    response = _upload(client, project_id, "EN", (name, content))

    assert response.status_code == 400
    assert not (
        project_storage.consent_attachments_path(project_id) / "EN" / name
    ).exists()


def test_a_language_without_pdfs_gets_the_default_language_s(setup):
    client, project_id = setup
    _upload(client, project_id, "EN", ("Information.pdf", PDF))

    assert [a["filename"] for a in _respondent(client, project_id, "DA").json()] == [
        "Information.pdf"
    ]
    opened = client.get(
        f"/api/projects/{project_id}/guide/consent/DA/attachments/Information.pdf",
        params={"draft": True},
    )
    assert opened.status_code == 200
    assert opened.content == PDF
    assert opened.headers["content-type"] == "application/pdf"
    assert opened.headers["x-content-type-options"] == "nosniff"
    assert opened.headers["content-disposition"].startswith("inline;")


def test_a_language_with_pdfs_gets_only_its_own(setup):
    client, project_id = setup
    _upload(client, project_id, "EN", ("Information.pdf", PDF))
    _upload(client, project_id, "DA", ("Information DA.pdf", PDF))

    assert [a["filename"] for a in _respondent(client, project_id, "DA").json()] == [
        "Information DA.pdf"
    ]
    # And the default's file is not reachable under the other language.
    assert (
        client.get(
            f"/api/projects/{project_id}/guide/consent/DA/attachments/Information.pdf",
            params={"draft": True},
        ).status_code
        == 404
    )


def test_respondents_need_a_published_project(setup):
    client, project_id = setup
    _upload(client, project_id, "EN", ("Information.pdf", PDF))
    client.cookies.clear()

    response = client.get(f"/api/projects/{project_id}/guide/consent/EN/attachments")

    assert response.status_code == 409
    assert response.json()["code"] == "not_published"


def test_a_language_the_project_does_not_offer_is_a_404(setup):
    client, project_id = setup

    assert _respondent(client, project_id, "FR").status_code == 404


def test_removing_an_unpublished_language_removes_its_pdfs(setup):
    client, project_id = setup
    _upload(client, project_id, "DA", ("Information DA.pdf", PDF))

    response = client.request(
        "DELETE", f"/api/projects/{project_id}/available_languages", json="DA"
    )

    assert response.status_code == 200, response.text
    assert not (project_storage.consent_attachments_path(project_id) / "DA").exists()


def _languages(draft, published):
    """A repository offering `draft` in the draft and `published` in the
    published version (None: never published)."""

    def get_available_languages_optimized(_project_id, source="draft"):
        codes = draft if source == "draft" else published
        if codes is None:
            raise NotPublishedError("Not published")
        return [{"code": code} for code in codes]

    return SimpleNamespace(
        projects=SimpleNamespace(
            get_available_languages_optimized=get_available_languages_optimized
        )
    )


@pytest.mark.parametrize(
    ("draft", "published", "kept"),
    [
        # Removed from the draft, still offered to respondents: kept.
        (["EN"], ["EN", "DA"], {"EN", "DA"}),
        # Offered by neither: gone.
        (["EN"], ["EN"], {"EN"}),
        (["EN"], None, {"EN"}),
        # Added to the draft, not yet published: kept.
        (["EN", "DA"], ["EN"], {"EN", "DA"}),
    ],
)
def test_a_language_s_pdfs_stay_while_the_draft_or_published_version_has_it(
    tmp_path, monkeypatch, draft, published, kept
):
    monkeypatch.setattr(lib_settings.storage, "base_path", tmp_path)
    project_id = uuid4()
    base = project_storage.consent_attachments_path(project_id)
    for language in ("EN", "DA"):
        (base / language).mkdir(parents=True)
        (base / language / "Information.pdf").write_bytes(PDF)

    prune_consent_attachments(_languages(draft, published), project_id)

    assert {p.name for p in base.iterdir()} == kept


def test_too_many_pdfs_at_once_are_refused_before_any_is_stored(setup):
    client, project_id = setup
    nine_mb = PDF + b"0" * (9 * 1024 * 1024)

    response = _upload(
        client,
        project_id,
        "EN",
        *((f"{n}.pdf", nine_mb) for n in range(3)),
    )

    assert response.status_code == 413
    assert not (project_storage.consent_attachments_path(project_id) / "EN").exists()


def test_replacing_a_pdf_counts_only_the_new_one(setup):
    client, project_id = setup
    eight_mb = PDF + b"0" * (8 * 1024 * 1024)
    for n in range(3):
        assert (
            _upload(client, project_id, "EN", (f"{n}.pdf", eight_mb)).status_code == 200
        )

    # 24 MB stored; replacing one 8 MB file with another stays at 24.
    response = _upload(client, project_id, "EN", ("0.pdf", eight_mb))

    assert response.status_code == 200, response.text


def test_a_clone_keeps_the_pdfs(setup):
    client, project_id = setup
    _upload(client, project_id, "DA", ("Information DA.pdf", PDF))

    clone = client.post(f"/api/projects/{project_id}/clone")

    assert clone.status_code == 200, clone.text
    copied = (
        project_storage.consent_attachments_path(clone.json()["id"])
        / "DA"
        / "Information DA.pdf"
    )
    assert copied.read_bytes() == PDF
