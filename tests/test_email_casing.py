"""An address is one address in any casing.

Only the domain used to be lowercased (the v1-style `CustomEmailStr` hook never
ran under pydantic v2), and accounts were found by exact match: someone who
registered as `Ada@example.com` could not sign in as `ada@example.com`, and the
two could be separate accounts. Every address is now stored lowercased
(`normalize_email`) and looked up the same way.

Runs against an in-memory SQLite database.
"""

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.crud import InterviewDataBase
from app.db.repositories.errors import EmailTakenError
from app.db.tables import Base
from app.schemas.projects import CollaboratorInviteCreate
from app.schemas.users import (
    AccessRequestCreate,
    ForgotPasswordRequest,
    LoginRequest,
    RegisterRequest,
    UpdateEmailRequest,
    UserCreate,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield InterviewDataBase(session)


def make_user(db, email):
    return db.users.create_user(
        UserCreate(email=email, first_name="Ada", password="pw", invite_token=None)
    )


def test_an_account_is_stored_lowercased(db):
    user = make_user(db, "  Ada.Lovelace@Example.COM ")

    assert user.email == "ada.lovelace@example.com"


def test_an_account_is_found_in_any_casing(db):
    make_user(db, "Ada@Example.com")

    login = LoginRequest(email="ADA@example.COM", password="pw")
    assert db.users.get_user_private(login.email).email == "ada@example.com"
    # A caller that skips the schema is normalised by the repository too.
    assert db.users.get_user_private("aDa@EXAMPLE.com").email == "ada@example.com"


def test_two_casings_cannot_be_two_accounts(db):
    make_user(db, "ada@example.com")

    with pytest.raises(EmailTakenError):
        make_user(db, "ADA@example.com")


@pytest.mark.parametrize(
    "model, field, extra",
    [
        (
            RegisterRequest,
            "email",
            {"first_name": "A", "password": "longenough1!", "invite_token": None},
        ),
        (AccessRequestCreate, "email", {"name": "A"}),
        (UpdateEmailRequest, "new_email", {"password": "pw"}),
        (CollaboratorInviteCreate, "email", {"role": "viewer"}),
        (ForgotPasswordRequest, "email", {}),
    ],
)
def test_requests_normalise_the_address(model, field, extra):
    request = model.model_validate({field: " Ada@Example.COM", **extra})

    assert getattr(request, field) == "ada@example.com"


def test_an_address_that_is_stored_is_still_validated():
    with pytest.raises(ValidationError):
        AccessRequestCreate(name="A", email="not an address")


def test_a_malformed_login_fails_like_a_wrong_one():
    """Login only looks the address up, so a malformed one finds nothing
    rather than answering 422."""
    login = LoginRequest(email="Not An Address", password="pw")

    assert login.email == "not an address"


def test_access_requests_and_invitations_are_stored_lowercased(db):
    db.users.create_access_request(
        AccessRequestCreate(name="A", email="Req@Example.com")
    )
    invitation = db.users.create_invitation(email="Inv@Example.com")

    assert [r.email for r in db.users.get_access_requests()] == ["req@example.com"]
    assert invitation.email == "inv@example.com"


def test_changing_email_stores_it_lowercased(db):
    user = make_user(db, "ada@example.com")

    assert (
        db.users.update_user_email(user.id, "New@Example.com").email
        == "new@example.com"
    )
    assert db.users.email_in_use("NEW@example.com")


def test_newsletter_subscriptions_are_one_per_address(db):
    first = db.newsletter.subscribe("News@Example.com")
    second = db.newsletter.subscribe("news@example.com")

    assert first.id == second.id
    assert db.newsletter.unsubscribe("NEWS@EXAMPLE.COM")
