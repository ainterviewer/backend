"""Shared test links: anyone holding one may try a project's draft as a
manual test, without an account, until it expires or is revoked.

Driven through the app with an in-memory database, as
test_content_versions_api.py is: what is pinned here is who the public
routes let read the draft and start a test.
"""

from datetime import timedelta
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from jose import JWTError, jwt
from pydantic import ValidationError
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ainterviewer.utils import now
from app.auth import AuthToken
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
from app.settings import app_settings
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
        owner = UserTable(email="a@example.com", password="x", first_name="Ada")
        viewer = UserTable(email="b@example.com", password="x", first_name="Bo")
        folder = ProjectFolderTable(title="f")
        session.add_all([owner, viewer, folder])
        session.flush()
        session.add_all(
            [
                CollaboratorTable(
                    folder_id=folder.id, user_id=owner.id, role=CollaboratorRole.EDITOR
                ),
                CollaboratorTable(
                    folder_id=folder.id, user_id=viewer.id, role=CollaboratorRole.VIEWER
                ),
            ]
        )
        session.commit()
        db = InterviewDataBase(session)
        project_id = db.projects.create_project(
            folder_id=folder.id, title="p", owner_id=owner.id, default_language="EN"
        )
        other_project_id = db.projects.create_project(
            folder_id=folder.id, title="q", owner_id=owner.id, default_language="EN"
        )
        owner_id, viewer_id = owner.id, viewer.id

    def _db():
        with Session(engine) as session:
            yield InterviewDataBase(session)

    app.dependency_overrides[get_db] = _db
    client = TestClient(app)
    editor = {"access_token": AuthToken(user_id=owner_id, scope=Scope.USER).encode()}
    viewer = {"access_token": AuthToken(user_id=viewer_id, scope=Scope.USER).encode()}
    yield client, engine, project_id, other_project_id, owner_id, editor, viewer
    app.dependency_overrides.clear()


def _mint(client, project_id, cookies) -> str:
    response = client.post(f"/api/projects/{project_id}/test_share", cookies=cookies)
    assert response.status_code == 200, response.text
    url = urlparse(response.json()["url"])
    assert url.path == "/interview"
    query = parse_qs(url.query)
    assert query["id"] == [str(project_id)]
    assert query["interview_type"] == ["manual_test"]
    return query["share"][0]


def _languages(client, project_id, share=None):
    params: dict = {"draft": True}
    if share is not None:
        params["share"] = share
    return client.get(f"/api/projects/{project_id}/available_languages", params=params)


def _start(client, project_id, share, interview_type="manual_test"):
    return client.post(
        f"/api/projects/{project_id}/EN/interviews",
        json={"interview_type": interview_type, "share_token": share},
    )


def _assert_invalid(response):
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "test_share_invalid"


def test_a_shared_link_runs_the_draft_without_an_account(setup):
    client, engine, project_id, _, _, editor, _ = setup
    share = _mint(client, project_id, editor)

    # Unpublished, so respondents get nothing; the link reads the draft.
    assert _languages(client, project_id).status_code == 401
    languages = _languages(client, project_id, share)
    assert languages.status_code == 200
    assert [lang["code"] for lang in languages.json()] == ["EN"]
    for path in ("guide/consent/EN", "guide/welcome/EN", "models", "config"):
        response = client.get(
            f"/api/projects/{project_id}/{path}",
            params={"draft": True, "share": share},
        )
        assert response.status_code == 200, (path, response.text)

    assert _start(client, project_id, share).status_code == 200

    with Session(engine) as session:
        interview = session.execute(select(InterviewTable)).scalar_one()
        assert interview.via_share_link

    listed = client.get(
        f"/api/projects/{project_id}/interviews",
        params={"interview_types": ["manual_test"]},
        cookies=editor,
    )
    (row,) = listed.json()["items"]
    assert row["via_share_link"] is True


def test_a_collaborators_own_test_is_not_marked_shared(setup):
    client, engine, project_id, _, _, editor, _ = setup
    created = client.post(
        f"/api/projects/{project_id}/EN/interviews",
        json={"interview_type": "manual_test"},
        cookies=editor,
    )
    assert created.status_code == 200
    with Session(engine) as session:
        assert not session.execute(select(InterviewTable)).scalar_one().via_share_link


def test_revoking_ends_every_link_handed_out(setup):
    client, _, project_id, _, _, editor, _ = setup
    first = _mint(client, project_id, editor)
    second = _mint(client, project_id, editor)
    assert _languages(client, project_id, first).status_code == 200

    revoked = client.delete(f"/api/projects/{project_id}/test_share", cookies=editor)
    assert revoked.status_code == 204

    for share in (first, second):
        _assert_invalid(_languages(client, project_id, share))
        _assert_invalid(_start(client, project_id, share))

    # A new link works; the revoked ones stay dead.
    fresh = _mint(client, project_id, editor)
    assert _languages(client, project_id, fresh).status_code == 200
    _assert_invalid(_languages(client, project_id, first))


def test_a_link_opens_only_its_own_project(setup):
    client, _, project_id, other_project_id, _, editor, _ = setup
    share = _mint(client, project_id, editor)
    _mint(client, other_project_id, editor)

    _assert_invalid(_languages(client, other_project_id, share))
    _assert_invalid(_start(client, other_project_id, share))


def test_a_malformed_or_expired_link_is_refused(setup):
    client, _, project_id, _, _, editor, _ = setup
    share = _mint(client, project_id, editor)
    claims = jwt.get_unverified_claims(share)
    secret = app_settings.secrets.jwt_secret_key.get_secret_value()

    expired = jwt.encode(
        {**claims, "exp": now() - timedelta(minutes=1)}, secret, algorithm="HS256"
    )
    forged = jwt.encode(claims, "not-the-secret", algorithm="HS256")
    for bad in ("nonsense", expired, forged):
        _assert_invalid(_languages(client, project_id, bad))
        _assert_invalid(_start(client, project_id, bad))


def test_a_link_is_no_session(setup):
    """Signed with the same key as sessions, but never mistaken for one."""
    client, _, project_id, _, _, editor, _ = setup
    share = _mint(client, project_id, editor)

    with pytest.raises((JWTError, ValidationError)):
        AuthToken.decode(share)
    refused = client.get(
        f"/api/projects/{project_id}/versions/status",
        cookies={"access_token": share},
    )
    assert refused.status_code == 401


def test_a_link_starts_manual_tests_only(setup):
    client, _, project_id, _, _, editor, _ = setup
    share = _mint(client, project_id, editor)
    assert _start(client, project_id, share, "synthetic_test").status_code == 400


def test_only_editors_may_share(setup):
    client, _, project_id, _, _, _, viewer = setup
    url = f"/api/projects/{project_id}/test_share"
    assert client.post(url).status_code == 401
    assert client.post(url, cookies=viewer).status_code == 403
    assert client.delete(url, cookies=viewer).status_code == 403


def test_demo_accounts_cannot_share(setup):
    client, engine, project_id, _, owner_id, editor, _ = setup
    demo = {"access_token": AuthToken(user_id=owner_id, scope=Scope.DEMO).encode()}
    refused = client.post(f"/api/projects/{project_id}/test_share", cookies=demo)
    assert refused.status_code == 403
    assert refused.json()["code"] == "demo_restricted"

    # A link minted before the owner became a demo account stops working:
    # a project a demo account owns may not be distributed.
    share = _mint(client, project_id, editor)
    with Session(engine) as session:
        session.execute(
            update(UserTable).where(UserTable.id == owner_id).values(scope=Scope.DEMO)
        )
        session.commit()
    _assert_invalid(_languages(client, project_id, share))
    _assert_invalid(_start(client, project_id, share))

    refused = client.post(f"/api/projects/{project_id}/test_share", cookies=editor)
    assert refused.status_code == 403
    assert refused.json()["code"] == "demo_restricted"


def test_an_unknown_project_is_refused(setup):
    client, _, project_id, _, _, editor, _ = setup
    share = _mint(client, project_id, editor)
    _assert_invalid(_languages(client, uuid4(), share))
