"""An invitation can carry a readable alias that stands in for its id in the
sign-up link; registering with the alias redeems the invitation like the id.

Runs against an in-memory SQLite database with email sending stubbed out.
"""

import asyncio
import json
import uuid
from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ainterviewer.utils import now
from app.api import auth as auth_api
from app.db.crud import InterviewDataBase
from app.db.repositories.errors import InvitationAliasTakenError
from app.db.tables import Base, UserTable
from app.schemas.users import InvitationCreate, InvitationUpdate, UserCreateRequest
from app.types import Scope


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


def invite(db, **fields):
    return db.users.create_invitation(**InvitationCreate(**fields).model_dump())


def register(db, token, email="ada@example.com"):
    request = UserCreateRequest(
        email=email, first_name="Ada", password="a-password", invite_token=token
    )
    response = asyncio.run(auth_api.register(request, db))
    return response.status_code, json.loads(bytes(response.body))


def registered(db, email="ada@example.com") -> UserTable:
    return db.session.query(UserTable).filter_by(email=email).one()


def test_alias_is_normalised_and_used_in_the_link(db):
    invitation = invite(db, alias="  Workshop-2026 ", reuseable=True)

    assert invitation.alias == "workshop-2026"
    assert invitation.invitation_link.endswith("/sign-up?token=workshop-2026")


def test_link_falls_back_to_the_id(db):
    invitation = invite(db)

    assert invitation.invitation_link.endswith(f"/sign-up?token={invitation.id}")


@pytest.mark.parametrize(
    "alias",
    ["x", "has space", "-leading", str(uuid.uuid4()), uuid.uuid4().hex, "a" * 65],
)
def test_alias_rejects_invalid_values(alias):
    with pytest.raises(ValidationError):
        InvitationCreate(alias=alias)


def test_register_with_alias(db):
    invitation = invite(
        db,
        alias="workshop",
        reuseable=True,
        user_scope=Scope.DEMO,
        title="Workshop",
        user_demo_daily_limit=3,
    )

    status, _ = register(db, "WORKSHOP")

    assert status == 201
    user = registered(db)
    assert user.scope == Scope.DEMO
    assert user.invite_token == invitation.id
    assert user.registration_token == "workshop"
    assert user.invitation_title == "Workshop"
    assert user.demo_daily_limit == 3


def test_reusable_alias_redeems_more_than_once(db):
    invite(db, alias="workshop", reuseable=True)

    assert register(db, "workshop", "a@example.com")[0] == 201
    assert register(db, "workshop", "b@example.com")[0] == 201


def test_single_use_alias_is_consumed(db):
    invite(db, alias="once")

    assert register(db, "once", "a@example.com")[0] == 201
    assert register(db, "once", "b@example.com") == (
        406,
        {"detail": "Invalid invite token"},
    )


def test_register_with_id_records_no_alias(db):
    invitation = invite(db, alias="workshop", reuseable=True)

    assert register(db, str(invitation.id))[0] == 201
    assert registered(db).registration_token is None


def test_unknown_alias_is_rejected(db):
    assert register(db, "nobody") == (406, {"detail": "Invalid invite token"})


def test_expired_alias_is_rejected_and_kept(db):
    invitation = invite(db, alias="late", expires_at=now() - timedelta(minutes=1))

    assert register(db, "late") == (406, {"detail": "Invite token expired"})
    # An expired single-use invitation is not consumed by the failed attempt.
    assert db.users.check_invite_token(invitation.id).alias == "late"


def test_duplicate_alias_on_create(db):
    invite(db, alias="taken")

    with pytest.raises(InvitationAliasTakenError):
        invite(db, alias="TAKEN")


def test_duplicate_alias_on_update(db):
    invite(db, alias="taken")
    other = invite(db, alias="free")

    with pytest.raises(InvitationAliasTakenError):
        db.users.update_invitation(other.id, InvitationUpdate(alias="taken"))
    assert db.users.check_invite_token(other.id).alias == "free"


def test_update_can_clear_the_alias(db):
    invitation = invite(db, alias="workshop")

    updated = db.users.update_invitation(invitation.id, InvitationUpdate(alias=None))

    assert updated.alias is None


def test_other_integrity_errors_are_not_reported_as_the_alias(db):
    invitation = invite(db, alias="mine")
    db.users.update_invitation(invitation.id, InvitationUpdate(email="a@example.com"))
    other = invite(db)

    with pytest.raises(Exception) as error:
        db.users.update_invitation(
            other.id, InvitationUpdate(email="a@example.com", alias="also-mine")
        )
    assert not isinstance(error.value, InvitationAliasTakenError)
