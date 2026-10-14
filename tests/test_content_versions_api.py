"""The versions of a project's content, over HTTP: what respondents are
served, and that concurrent editors cannot overwrite each other.

Driven through the app with the database swapped for an in-memory one, since
what is pinned here is the wiring -- headers, status codes, which content a
public route reads -- more than the repository behind it
(test_content_versions.py).
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.auth import AuthToken
from app.db import InterviewDataBase
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
        project_id = InterviewDataBase(session).projects.create_project(
            folder_id=folder.id, title="p", owner_id=user.id, default_language="EN"
        )
        user_id = user.id

    def _db():
        with Session(engine) as session:
            yield InterviewDataBase(session)

    app.dependency_overrides[get_db] = _db
    client = TestClient(app)
    token = AuthToken(user_id=user_id, scope=Scope.USER).encode()
    yield client, project_id, {"access_token": token}, engine
    app.dependency_overrides.clear()


def _publish(client, project_id, cookies):
    status = client.get(f"/api/projects/{project_id}/versions/status", cookies=cookies)
    return client.post(
        f"/api/projects/{project_id}/versions",
        json={"publish": True},
        headers={"If-Match": status.headers["ETag"]},
        cookies=cookies,
    )


def test_a_save_must_be_based_on_the_current_revision(setup):
    client, project_id, cookies, _ = setup
    url = f"/api/projects/{project_id}/guide"
    loaded = client.get(url, cookies=cookies)
    etag = loaded.headers["ETag"]
    edited = {**loaded.json(), "introduction": {"EN": "Hello"}}

    assert client.put(url, json=edited, cookies=cookies).status_code == 428

    saved = client.put(url, json=edited, headers={"If-Match": etag}, cookies=cookies)
    assert saved.status_code == 200
    assert saved.headers["ETag"] != etag

    # A second editor still holding the first revision.
    stale = client.put(url, json=edited, headers={"If-Match": etag}, cookies=cookies)
    assert stale.status_code == 412
    assert stale.json()["code"] == "stale_revision"
    assert f'"{stale.json()["current_revision"]}"' == saved.headers["ETag"]


def test_respondents_get_nothing_until_the_project_is_published(setup):
    client, project_id, cookies, _ = setup
    languages = f"/api/projects/{project_id}/available_languages"

    refused = client.get(languages)
    assert refused.status_code == 409
    assert refused.json()["code"] == "not_published"
    assert client.get(f"/api/projects/{project_id}/guide/consent/EN").status_code == 409

    # The draft is for the project's collaborators only.
    assert client.get(languages, params={"draft": True}).status_code == 401
    draft = client.get(languages, params={"draft": True}, cookies=cookies)
    assert [lang["code"] for lang in draft.json()] == ["EN"]

    published = _publish(client, project_id, cookies)
    assert published.status_code == 200
    assert published.json()["semver"] == "1.0.0"
    assert client.get(languages).status_code == 200


def test_distributed_interviews_run_the_published_version(setup):
    client, project_id, cookies, engine = setup
    create = f"/api/projects/{project_id}/EN/interviews"

    assert client.post(create, json={}).status_code == 409

    version_id = _publish(client, project_id, cookies).json()["id"]
    assert client.post(create, json={}).status_code == 200

    # A test runs the draft: with nothing changed, that is the published one.
    test = client.post(create, json={"interview_type": "manual_test"}, cookies=cookies)
    assert test.status_code == 200

    with Session(engine) as session:
        versions = set(
            session.execute(select(InterviewTable.content_version_id)).scalars()
        )
    assert {str(v) for v in versions} == {version_id}


def test_publishing_reports_what_blocks_it(setup):
    client, project_id, cookies, _ = setup
    client.post(
        f"/api/projects/{project_id}/available_languages",
        json={"language": "DA", "translate": False},
        cookies=cookies,
    )
    url = f"/api/projects/{project_id}/guide"
    loaded = client.get(url, cookies=cookies)
    guide = loaded.json()
    guide["introduction"] = {"EN": "Hello"}
    client.put(
        url, json=guide, headers={"If-Match": loaded.headers["ETag"]}, cookies=cookies
    )

    refused = _publish(client, project_id, cookies)

    assert refused.status_code == 409
    assert refused.json()["code"] == "cannot_publish"
    assert "guide.introduction" in refused.json()["missing"]["DA"]
