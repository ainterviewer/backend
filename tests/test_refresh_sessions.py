"""Sessions: refresh-token rotation, its reuse grace, "remember me" and
account expiry.

One browser routinely refreshes several times at once with the same token, so
a token spent moments ago may be spent again; one spent longer ago is a
replay and ends its family. An extended session's refresh cookie outlives the
browser. An expired account can neither sign in nor refresh.

Calls the route handlers directly against an in-memory SQLite database, with
email sending stubbed out.
"""

import asyncio
from datetime import timedelta

import pytest
from fastapi import HTTPException, Response
from sqlalchemy import create_engine, update
from sqlalchemy.orm import Session

from ainterviewer.utils import now
from app.api import auth as auth_api
from app.auth import hash_token
from app.db.crud import InterviewDataBase
from app.db.tables import Base, RefreshTokenTable, UserTable
from app.errors import AppError
from app.schemas.users import LoginRequest, UserCreate
from app.settings import app_settings
from app.types import ErrorCode

EMAIL = "researcher@example.com"
PASSWORD = "a-password"
_EXTENDED = app_settings.app.jwt_refresh_token_extended_expiration.to_timedelta()
REMEMBERED = f"Max-Age={int(_EXTENDED.total_seconds())}"


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield InterviewDataBase(session)


@pytest.fixture
def user(db):
    """A verified user without two-factor, so a login starts a session."""
    created = db.users.create_user(
        UserCreate(email=EMAIL, first_name="Ada", password=PASSWORD, invite_token=None)
    )
    db.users.set_email_verified(created.id)
    db.session.execute(
        update(UserTable)
        .where(UserTable.id == created.id)
        .values(two_factor_enabled=False)
    )
    db.session.commit()
    return created


@pytest.fixture
def sent(monkeypatch) -> list[str]:
    """Capture outgoing emails instead of sending them."""
    outbox: list[str] = []

    async def fake_send_email(recipient, subject, **kwargs):
        outbox.append(subject)

    monkeypatch.setattr(auth_api, "send_email", fake_send_email)
    return outbox


def cookies(response: Response) -> dict[str, str]:
    """Each Set-Cookie header by cookie name."""
    return {
        header.split("=", 1)[0]: header
        for header in response.headers.getlist("set-cookie")
    }


def refresh_cookie(response: Response) -> str:
    return cookies(response)["refresh_token"].split(";", 1)[0].split("=", 1)[1]


def login(db, extended=False) -> Response:
    response = Response()
    asyncio.run(
        auth_api.login(
            LoginRequest(email=EMAIL, password=PASSWORD, extended=extended),
            db,
            response,
        )
    )
    return response


def refresh(db, token: str) -> Response:
    response = Response()
    asyncio.run(auth_api.refresh(db, response, token))
    return response


def stored(db, token: str) -> RefreshTokenTable:
    found = db.auth.get_by_token_hash(hash_token(token))
    assert found is not None
    return found


def spent_ago(db, token: str, ago: timedelta | None) -> None:
    """Backdate when `token` was spent; None as if before `used_at` existed."""
    db.session.execute(
        update(RefreshTokenTable)
        .where(RefreshTokenTable.token_hash == hash_token(token))
        .values(used_at=None if ago is None else now() - ago)
    )
    db.session.commit()


def family_revoked(db, token: str) -> bool:
    family = stored(db, token).family_id
    db.session.expire_all()
    rows = (
        db.session.query(RefreshTokenTable)
        .filter(RefreshTokenTable.family_id == family)
        .all()
    )
    return all(row.is_revoked for row in rows)


class TestReuseGrace:
    def test_a_token_spent_moments_ago_gets_a_pair_of_its_own(self, db, user):
        first = refresh_cookie(login(db))

        winner = refresh_cookie(refresh(db, first))
        loser = refresh_cookie(refresh(db, first))

        assert winner != loser
        assert not family_revoked(db, first)
        # Both successors are live: whichever cookie the browser kept works.
        refresh(db, winner)
        refresh(db, loser)

    def test_a_token_spent_long_ago_ends_the_family(self, db, user):
        first = refresh_cookie(login(db))
        successor = refresh_cookie(refresh(db, first))
        spent_ago(db, first, timedelta(minutes=5))

        with pytest.raises(HTTPException) as refused:
            refresh(db, first)

        assert refused.value.status_code == 401
        assert family_revoked(db, successor)

    def test_a_token_spent_before_used_at_existed_counts_as_long_ago(self, db, user):
        first = refresh_cookie(login(db))
        refresh(db, first)
        spent_ago(db, first, None)

        with pytest.raises(HTTPException):
            refresh(db, first)

        assert family_revoked(db, first)

    def test_logout_ends_the_predecessor_still_in_its_grace(self, db, user):
        first = refresh_cookie(login(db))
        current = refresh_cookie(refresh(db, first))

        asyncio.run(auth_api.logout(db, Response(), current))

        with pytest.raises(HTTPException) as refused:
            refresh(db, first)
        assert refused.value.status_code == 401


class TestRememberMe:
    def test_an_extended_session_keeps_its_refresh_cookie_past_the_browser(
        self, db, user
    ):
        set_cookies = cookies(login(db, extended=True))

        assert REMEMBERED in set_cookies["refresh_token"]
        assert "Max-Age" not in set_cookies["access_token"]

    def test_any_other_session_has_session_cookies(self, db, user):
        set_cookies = cookies(login(db))

        assert "Max-Age" not in set_cookies["refresh_token"]
        assert "Max-Age" not in set_cookies["access_token"]

    def test_a_refresh_keeps_the_session_extended(self, db, user):
        first = refresh_cookie(login(db, extended=True))

        assert REMEMBERED in cookies(refresh(db, first))["refresh_token"]


class TestAccountExpiry:
    def expire(self, db, user):
        db.session.execute(
            update(UserTable)
            .where(UserTable.id == user.id)
            .values(expires_at=now() - timedelta(minutes=1))
        )
        db.session.commit()

    def test_an_expired_account_cannot_sign_in(self, db, user):
        self.expire(db, user)

        with pytest.raises(AppError) as refused:
            login(db)

        assert refused.value.status == 403
        assert refused.value.code == ErrorCode.ACCOUNT_EXPIRED

    def test_an_expired_account_is_sent_no_login_code(self, db, user, sent):
        db.session.execute(
            update(UserTable)
            .where(UserTable.id == user.id)
            .values(two_factor_enabled=True)
        )
        self.expire(db, user)

        with pytest.raises(AppError):
            login(db)

        assert sent == []

    def test_an_account_that_expires_mid_session_cannot_refresh(self, db, user):
        first = refresh_cookie(login(db))
        self.expire(db, user)

        with pytest.raises(AppError) as refused:
            refresh(db, first)

        assert refused.value.status == 401
        assert refused.value.code == ErrorCode.ACCOUNT_EXPIRED
        assert family_revoked(db, first)

    def test_an_account_expiring_later_still_signs_in(self, db, user):
        db.session.execute(
            update(UserTable)
            .where(UserTable.id == user.id)
            .values(expires_at=now() + timedelta(days=1))
        )
        db.session.commit()

        assert "refresh_token" in cookies(login(db))
