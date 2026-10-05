"""Forgot-password emails a one-time magic link; redeeming it sets a new
password, verifies the address and ends every existing session.

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
from app.api.request_models import ForgotPasswordRequest, ResetPasswordRequest
from app.auth import hash_token, verify_password
from app.db.crud import InterviewDataBase
from app.db.models import UserCreate
from app.db.tables import Base

EMAIL = "researcher@example.com"
OLD_PASSWORD = "old-password"
NEW_PASSWORD = "new-password"


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield InterviewDataBase(session)


@pytest.fixture
def user(db):
    return db.users.create_user(
        UserCreate(
            email=EMAIL,
            first_name="Ada",
            password=OLD_PASSWORD,
            invite_token=None,
        )
    )


@pytest.fixture
def sent(monkeypatch) -> list[dict]:
    """Capture outgoing emails instead of sending them."""
    outbox: list[dict] = []

    async def fake_send_email(recipient, subject, **kwargs):
        outbox.append({"recipient": recipient, "subject": subject, **kwargs})

    monkeypatch.setattr(auth_api, "send_email", fake_send_email)
    return outbox


def forgot(db, email=EMAIL):
    return asyncio.run(auth_api.forgot_password(ForgotPasswordRequest(email=email), db))


def reset(db, token, password=NEW_PASSWORD):
    return asyncio.run(
        auth_api.reset_password(
            ResetPasswordRequest(token=token, new_password=password), db
        )
    )


def token_from(email: dict) -> str:
    html = email["html_content"]
    start = html.index("/reset-password?token=")
    link = html[start : html.index('"', start)]
    return parse_qs(urlparse(link).query)["token"][0]


def test_unknown_email_gets_the_same_answer_and_no_mail(db, user, sent):
    known = forgot(db)
    unknown = forgot(db, "nobody@example.com")

    assert known.status_code == unknown.status_code == 200
    assert known.body == unknown.body
    assert [m["recipient"] for m in sent] == [EMAIL]


def test_reset_sets_password_verifies_email_and_revokes_sessions(db, user, sent):
    db.auth.create_refresh_token(
        user_id=user.id,
        token_hash=hash_token("live-session"),
        family_id=uuid.uuid4(),
        expires_at=now() + timedelta(days=1),
    )
    forgot(db)

    response = reset(db, token_from(sent[0]))

    assert response.status_code == 200
    stored = db.users.get_user_private(EMAIL)
    assert verify_password(NEW_PASSWORD, stored.password)
    assert stored.email_verified
    assert db.auth.get_by_token_hash(hash_token("live-session")).is_revoked


def test_link_is_single_use(db, user, sent):
    forgot(db)
    token = token_from(sent[0])
    reset(db, token)

    with pytest.raises(HTTPException) as exc:
        reset(db, token, "another-password")
    assert exc.value.status_code == 400


def test_new_request_invalidates_the_previous_link(db, user, sent, monkeypatch):
    monkeypatch.setattr(auth_api.app_settings.app, "code_resend_cooldown_seconds", 0)
    forgot(db)
    forgot(db)
    first, second = token_from(sent[0]), token_from(sent[1])

    with pytest.raises(HTTPException):
        reset(db, first)
    reset(db, second)


def test_requests_within_cooldown_send_nothing(db, user, sent):
    forgot(db)
    forgot(db)

    assert len(sent) == 1


def test_expired_link_is_rejected(db, user, sent, monkeypatch):
    forgot(db)
    token = token_from(sent[0])
    monkeypatch.setattr(
        "app.db.repositories.verification.now", lambda: now() + timedelta(days=1)
    )

    with pytest.raises(HTTPException) as exc:
        reset(db, token)
    assert exc.value.status_code == 400
    assert verify_password(OLD_PASSWORD, db.users.get_user_private(EMAIL).password)
