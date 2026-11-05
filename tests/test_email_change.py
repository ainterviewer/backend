"""Changing an account's email takes a link sent to the new address.

`PATCH /me/email` used to write the new address straight onto the account,
unverified: anyone signed in could take an address they did not own (and keep
its owner from registering), appear under it to collaborators, lock themselves
out with a typo now that sign-in codes go by email, and -- because a taken
address answered `email_taken` -- test which addresses have accounts.

Now the request changes nothing and answers the same whether or not the address
is taken; the link makes the change, tells the old address, and ends the
account's other sessions.

Runs against an in-memory SQLite database with email sending stubbed out.
"""

import asyncio
import uuid
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ainterviewer.utils import now
from app.api import auth as auth_api
from app.auth import AuthToken, hash_token
from app.db.crud import InterviewDataBase
from app.db.repositories.errors import EmailTakenError
from app.db.tables import Base
from app.errors import AppError
from app.schemas.users import ConfirmEmailChangeRequest, UpdateEmailRequest, UserCreate
from app.types import ErrorCode, Scope

EMAIL = "researcher@example.com"
NEW_EMAIL = "new@example.com"
PASSWORD = "password"


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield InterviewDataBase(session)


def make_user(db, email):
    return db.users.create_user(
        UserCreate(email=email, first_name="Ada", password=PASSWORD, invite_token=None)
    )


@pytest.fixture
def user(db):
    return make_user(db, EMAIL)


@pytest.fixture
def sent(monkeypatch) -> list[dict]:
    outbox: list[dict] = []

    async def fake_send_email(recipient, subject, **kwargs):
        outbox.append({"recipient": recipient, "subject": subject, **kwargs})

    monkeypatch.setattr(auth_api, "send_email", fake_send_email)
    return outbox


def request_change(db, user, new_email=NEW_EMAIL, password=PASSWORD):
    return asyncio.run(
        auth_api.update_my_email(
            UpdateEmailRequest(password=password, new_email=new_email),
            db,
            AuthToken(user_id=user.id, scope=Scope.USER),
        )
    )


def confirm(db, token, refresh=None):
    return asyncio.run(
        auth_api.confirm_email_change(
            ConfirmEmailChangeRequest(token=token), db, refresh
        )
    )


def token_from(email: dict) -> str:
    html = email["html_content"]
    start = html.index("/confirm-email-change?token=")
    link = html[start : html.index('"', start)]
    return parse_qs(urlparse(link).query)["token"][0]


def session(db, user, raw):
    db.auth.create_refresh_token(
        user_id=user.id,
        token_hash=hash_token(raw),
        family_id=uuid.uuid4(),
        expires_at=now() + timedelta(days=1),
    )


def test_requesting_changes_nothing_until_the_link_is_followed(db, user, sent):
    request_change(db, user)

    assert db.users.get_user_by_id(user.id).email == EMAIL
    assert [m["recipient"] for m in sent] == [NEW_EMAIL]


def test_a_taken_address_gets_the_same_answer_and_no_mail(db, user, sent):
    make_user(db, "Taken@example.com")

    free = request_change(db, user, NEW_EMAIL)
    taken = request_change(db, user, "taken@example.com")

    assert free.message.replace(NEW_EMAIL, "X") == taken.message.replace(
        "taken@example.com", "X"
    )
    assert [m["recipient"] for m in sent] == [NEW_EMAIL]


def test_the_link_changes_the_email_and_tells_the_old_address(db, user, sent):
    request_change(db, user)

    confirm(db, token_from(sent[0]))

    changed = db.users.get_user_private(user_id=user.id)
    assert changed.email == NEW_EMAIL
    assert changed.email_verified
    assert sent[1]["recipient"] == EMAIL
    assert NEW_EMAIL in sent[1]["html_content"]


def test_the_change_ends_other_sessions_but_keeps_the_confirming_one(db, user, sent):
    session(db, user, "this-browser")
    session(db, user, "elsewhere")
    request_change(db, user)

    confirm(db, token_from(sent[0]), refresh="this-browser")

    assert not db.auth.get_by_token_hash(hash_token("this-browser")).is_revoked
    assert db.auth.get_by_token_hash(hash_token("elsewhere")).is_revoked


def test_confirming_signed_out_ends_every_session(db, user, sent):
    session(db, user, "elsewhere")
    request_change(db, user)

    confirm(db, token_from(sent[0]))

    assert db.auth.get_by_token_hash(hash_token("elsewhere")).is_revoked


def test_another_accounts_session_is_not_kept(db, user, sent):
    other = make_user(db, "other@example.com")
    session(db, other, "their-browser")
    session(db, user, "elsewhere")
    request_change(db, user)

    confirm(db, token_from(sent[0]), refresh="their-browser")

    assert db.auth.get_by_token_hash(hash_token("elsewhere")).is_revoked
    assert not db.auth.get_by_token_hash(hash_token("their-browser")).is_revoked


def test_the_link_is_single_use(db, user, sent):
    request_change(db, user)
    token = token_from(sent[0])
    confirm(db, token)

    with pytest.raises(AppError) as exc:
        confirm(db, token)
    assert exc.value.code == ErrorCode.EMAIL_LINK_INVALID


def test_a_new_request_invalidates_the_previous_link(db, user, sent):
    request_change(db, user, "first@example.com")
    request_change(db, user, "second@example.com")

    with pytest.raises(AppError):
        confirm(db, token_from(sent[0]))
    confirm(db, token_from(sent[1]))
    assert db.users.get_user_by_id(user.id).email == "second@example.com"


def test_an_address_taken_in_the_meantime_is_refused(db, user, sent):
    request_change(db, user)
    make_user(db, NEW_EMAIL)

    with pytest.raises(EmailTakenError):
        confirm(db, token_from(sent[0]))
    assert db.users.get_user_by_id(user.id).email == EMAIL


def test_a_wrong_password_is_refused(db, user, sent):
    with pytest.raises(HTTPException) as exc:
        request_change(db, user, password="wrong")
    assert exc.value.status_code == 403
    assert sent == []


def test_the_current_address_is_refused(db, user, sent):
    with pytest.raises(HTTPException) as exc:
        request_change(db, user, EMAIL.upper())
    assert exc.value.status_code == 400
    assert sent == []
