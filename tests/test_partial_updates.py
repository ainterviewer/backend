"""PATCH bodies tell a field left out from a field set to null.

Omitting a field leaves the column alone; sending null clears it where the
column allows it and is refused where it does not. The sentinel behind that is
pydantic's `MISSING`, which never reaches the OpenAPI schema -- unlike the
`"UNSET"` enum it replaced, which the generated client typed as a valid value.

Runs against an in-memory SQLite database.
"""

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.crud import InterviewDataBase
from app.db.models import UserCreate, UserCreateRequest, UserSelfUpdate
from app.db.tables import Base, UserTable
from app.main import app


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
            email="ada@example.com",
            first_name="Ada",
            last_name="Lovelace",
            organization="Analytical Engines",
            password="a-password",
            invite_token=None,
        )
    )


def stored(db, user) -> UserTable:
    row = db.session.get(UserTable, user.id)
    db.session.refresh(row)
    return row


def test_omitted_fields_are_left_alone(db, user):
    db.users.update_user(user.id, UserSelfUpdate(first_name="Augusta"))

    row = stored(db, user)
    assert row.first_name == "Augusta"
    assert row.last_name == "Lovelace"
    assert row.organization == "Analytical Engines"


def test_null_clears_a_nullable_field(db, user):
    db.users.update_user(user.id, UserSelfUpdate.model_validate({"last_name": None}))

    row = stored(db, user)
    assert row.last_name is None
    assert row.first_name == "Ada"


def test_null_is_refused_for_a_required_field():
    with pytest.raises(ValidationError):
        UserSelfUpdate.model_validate({"first_name": None})


def test_an_empty_update_changes_nothing(db, user):
    db.users.update_user(user.id, UserSelfUpdate())

    assert stored(db, user).first_name == "Ada"


def test_no_sentinel_reaches_the_schema():
    schemas = app.openapi()["components"]["schemas"]

    assert not [name for name in schemas if "Unset" in name]
    assert "UNSET" not in str(schemas)


def test_registration_carries_no_server_fields():
    """Scope and timestamps are the server's, so a client cannot send them."""
    request = UserCreateRequest.model_validate(
        {
            "email": "ada@example.com",
            "first_name": "Ada",
            "password": "a-password",
            "invite_token": None,
            "scope": "admin",
            "created_at": "2000-01-01T00:00:00Z",
        }
    )

    assert set(request.model_dump()) == {
        "email",
        "first_name",
        "last_name",
        "invite_token",
        "research_consent",
        "password",
    }
