"""Preview features are given one by one: a user's list, carried in their
token, checked per feature, and handed out by invitations.

Runs against an in-memory SQLite database with email sending stubbed out.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api import auth as auth_api
from app.auth import AuthToken, create_auth_token
from app.db.crud import InterviewDataBase
from app.db.tables import Base, UserTable
from app.dependencies import require_preview
from app.errors import AppError
from app.schemas.users import InvitationCreate, RegisterRequest, UserAdminUpdate
from app.types import ErrorCode, PreviewFeature, Scope


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield InterviewDataBase(session)


@pytest.fixture(autouse=True)
def no_email(monkeypatch):
    async def fake_send_email(recipient, subject, **kwargs):
        pass

    monkeypatch.setattr(auth_api, "send_email", fake_send_email)
    monkeypatch.setattr(auth_api.app_settings.app, "registration_requires_token", True)


def decoded(**claims) -> AuthToken:
    """A token as one minted with these claims decodes."""
    token = AuthToken(user_id=uuid.uuid4(), scope=Scope.USER)
    raw = token.model_dump(mode="json") | claims
    return AuthToken.model_validate(raw)


def test_the_token_round_trips_its_features():
    raw = create_auth_token(
        uuid.uuid4(), Scope.USER, [PreviewFeature.REPORT, PreviewFeature.AUDIO]
    )
    assert AuthToken.decode(raw).preview_features == [
        PreviewFeature.AUDIO,
        PreviewFeature.REPORT,
    ]


@pytest.mark.parametrize(
    ("flag", "expected"), [(True, list(PreviewFeature)), (False, [])]
)
def test_a_token_from_before_the_list_reads_as_all_or_none(flag, expected):
    assert sorted(decoded(preview_features=flag).preview_features) == sorted(expected)


def test_a_graduated_feature_in_a_token_is_ignored_not_refused():
    token = decoded(preview_features=["explore", "report", "report"])
    assert token.preview_features == [PreviewFeature.REPORT]


def test_require_preview_checks_the_one_feature():
    check = require_preview(PreviewFeature.CODEBOOK)
    allowed = decoded(preview_features=["codebook"])
    assert check(allowed) is allowed

    with pytest.raises(AppError) as refused:
        check(decoded(preview_features=["report"]))
    assert refused.value.code == ErrorCode.PREVIEW_FEATURES_REQUIRED


def test_registering_with_an_invitation_gives_its_features(db):
    invitation = db.users.create_invitation(
        **InvitationCreate(user_preview_features=["report", "audio"]).model_dump()
    )
    assert invitation.user_preview_features == [
        PreviewFeature.AUDIO,
        PreviewFeature.REPORT,
    ]

    request = RegisterRequest(
        email="ada@example.com",
        first_name="Ada",
        password="a-password",
        invite_token=invitation.id,
    )
    asyncio.run(auth_api.register(request, db))

    user = db.session.query(UserTable).filter_by(email="ada@example.com").one()
    assert user.preview_features == ["audio", "report"]


def test_an_admin_sets_a_users_features(db):
    invitation = db.users.create_invitation(**InvitationCreate().model_dump())
    request = RegisterRequest(
        email="ada@example.com",
        first_name="Ada",
        password="a-password",
        invite_token=invitation.id,
    )
    asyncio.run(auth_api.register(request, db))
    user = db.session.query(UserTable).filter_by(email="ada@example.com").one()
    assert user.preview_features == []

    db.users.update_user_admin(
        user.id, UserAdminUpdate(preview_features=["participants"])
    )
    db.session.refresh(user)
    assert user.preview_features == ["participants"]
