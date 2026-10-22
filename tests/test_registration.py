"""A registration that fails leaves its single-use invitation redeemable.

The invitation used to be deleted, and committed, before the account was
inserted, so a sign-up with an email already in use came back a 500 having
spent the invite, and a verification email that failed to send rolled back the
account but not the invitation.

Runs against an in-memory SQLite database with email sending stubbed out.
"""

import asyncio
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api import auth as auth_api
from app.db.crud import InterviewDataBase
from app.db.repositories.errors import EmailTakenError, InvitationClaimedError
from app.db.tables import Base, UserTable
from app.errors import AppError
from app.schemas.users import InvitationCreate, RegisterRequest, UserCreate
from app.types import ErrorCode


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield InterviewDataBase(session)


@pytest.fixture(autouse=True)
def requires_token(monkeypatch):
    monkeypatch.setattr(auth_api.app_settings.app, "registration_requires_token", True)


@pytest.fixture
def sent(monkeypatch):
    sent = []

    async def fake_send_email(recipient, subject, **kwargs):
        sent.append(recipient)

    monkeypatch.setattr(auth_api, "send_email", fake_send_email)
    return sent


@pytest.fixture
def email_down(monkeypatch):
    async def failing_send_email(recipient, subject, **kwargs):
        raise ConnectionError("SMTP unreachable")

    monkeypatch.setattr(auth_api, "send_email", failing_send_email)


def invite(db, **fields):
    return db.users.create_invitation(**InvitationCreate(**fields).model_dump())


def register(db, token, email="ada@example.com"):
    request = RegisterRequest(
        email=email, first_name="Ada", password="a-password", invite_token=token
    )
    response = asyncio.run(auth_api.register(request, db))
    return response.status_code, json.loads(bytes(response.body))


def accounts(db, email="ada@example.com") -> int:
    return db.session.query(UserTable).filter_by(email=email).count()


def test_email_in_use_is_a_409_and_keeps_the_invite(db, sent):
    first, second = invite(db), invite(db, alias="second")
    assert register(db, first.id)[0] == 201

    with pytest.raises(EmailTakenError) as error:
        register(db, second.id)

    assert error.value.status == 409
    assert db.users.check_invite_token(second.id).alias == "second"
    assert accounts(db) == 1


def test_failed_verification_email_restores_the_invite(db, email_down):
    invitation = invite(db, alias="once", title="Workshop", user_demo_daily_limit=3)

    with pytest.raises(auth_api.EmailSendError) as error:
        register(db, invitation.id)

    assert error.value.status == 502
    assert accounts(db) == 0
    restored = db.users.check_invite_token(invitation.id)
    assert restored.model_dump() == invitation.model_dump()


def test_restored_invite_registers_on_retry(db, monkeypatch, email_down):
    invitation = invite(db)
    with pytest.raises(auth_api.EmailSendError):
        register(db, invitation.id)

    async def fake_send_email(recipient, subject, **kwargs):
        pass

    monkeypatch.setattr(auth_api, "send_email", fake_send_email)

    assert register(db, invitation.id)[0] == 201
    assert accounts(db) == 1


def test_claiming_a_spent_invite_creates_no_account(db):
    """What the loser of two concurrent sign-ups on one token sees."""
    invitation = invite(db)
    db.users.delete_invitation(invitation.id)

    with pytest.raises(InvitationClaimedError):
        db.users.create_user(
            UserCreate(
                email="ada@example.com",
                first_name="Ada",
                password="a-password",
                invite_token=None,
            ),
            claim_invitation=invitation.id,
        )

    assert accounts(db) == 0


def test_single_use_invite_is_spent_by_a_registration(db, sent):
    invitation = invite(db)

    assert register(db, invitation.id)[0] == 201
    with pytest.raises(AppError) as error:
        register(db, invitation.id, "b@example.com")

    assert error.value.code == ErrorCode.INVITE_INVALID
    assert sent == ["ada@example.com"]
