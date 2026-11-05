"""Requesting access answers the same for every address.

A second request for an address answered 409 `access_request_exists`, which
told anyone who had already asked. Now a duplicate is dropped quietly, an
address that already has an account gets an email saying so instead of a
request, and a failed admin notification no longer turns a stored request into
a 500.

Runs against an in-memory SQLite database with email sending stubbed out.
"""

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api import auth as auth_api
from app.db.crud import InterviewDataBase
from app.db.tables import Base
from app.schemas.users import AccessRequestCreate, UserCreate
from app.settings import EmailAccount, EmailSettings

ADMIN_INBOX = "admins@example.com"


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield InterviewDataBase(session)


@pytest.fixture(autouse=True)
def email_settings(monkeypatch):
    """An admin inbox to notify. The route reads it from the settings, which
    CI leaves unset (`services.email` is None) and a developer's config may
    fill with anything."""
    account = EmailAccount(email=ADMIN_INBOX, password="unused")
    email = EmailSettings(smtp_server="smtp.invalid", sender=account, recipient=account)
    settings = auth_api.app_settings
    # The settings are frozen, so swap in a copy rather than editing them.
    monkeypatch.setattr(
        auth_api,
        "app_settings",
        settings.model_copy(
            update={"services": settings.services.model_copy(update={"email": email})}
        ),
    )


@pytest.fixture
def sent(monkeypatch) -> list[dict]:
    outbox: list[dict] = []

    async def fake_send_email(recipient, subject, **kwargs):
        outbox.append({"recipient": recipient, "subject": subject, **kwargs})

    monkeypatch.setattr(auth_api, "send_email", fake_send_email)
    return outbox


def request(db, email="ada@example.com"):
    return asyncio.run(
        auth_api.request_access(AccessRequestCreate(name="Ada", email=email), db)
    )


def test_a_new_request_is_stored_and_the_admins_told(db, sent):
    request(db)

    assert [r.email for r in db.users.get_access_requests()] == ["ada@example.com"]
    assert [m["recipient"] for m in sent] == [ADMIN_INBOX]


def test_a_duplicate_answers_the_same_and_notifies_nobody(db, sent):
    first = request(db)
    again = request(db, "ADA@example.com")

    assert again == first
    assert len(db.users.get_access_requests()) == 1
    assert [m["recipient"] for m in sent] == [ADMIN_INBOX]


def test_an_account_holder_is_told_they_have_one(db, sent):
    first = request(db, "new@example.com")
    db.users.create_user(
        UserCreate(
            email="ada@example.com", first_name="Ada", password="pw", invite_token=None
        )
    )

    assert request(db) == first
    assert [r.email for r in db.users.get_access_requests()] == ["new@example.com"]
    assert sent[-1]["recipient"] == "ada@example.com"
    assert "request access" in sent[-1]["html_content"]


def test_a_failed_admin_notification_keeps_the_request(db, monkeypatch):
    async def failing_send_email(*_args, **_kwargs):
        raise OSError("SMTP down")

    monkeypatch.setattr(auth_api, "send_email", failing_send_email)

    request(db)

    assert len(db.users.get_access_requests()) == 1
