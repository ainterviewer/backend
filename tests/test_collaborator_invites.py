"""Adding a folder collaborator sends an invite, and only accepting the emailed
link makes them one.

The inviter gets the same answer, and sees the same pending row, whether or not
the email has an account -- the form used to 500 on an unknown email, which told
any folder editor who was a user. The recipient's email differs: an account
holder gets a link to accept, anyone else is pointed at the access request form
first.

Runs against an in-memory SQLite database with email sending stubbed out.
"""

import asyncio
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, update
from sqlalchemy.orm import Session

from ainterviewer.utils import now
from app.api.auth import EmailSendError
from app.api.dashboard import folders as folders_api
from app.auth import AuthToken
from app.db.crud import InterviewDataBase
from app.db.repositories.errors import (
    AlreadyCollaboratorError,
    CollaboratorInviteExpiredError,
    CollaboratorInviteInvalidError,
)
from app.db.tables import Base, CollaboratorInviteTable
from app.schemas.projects import (
    CollaboratorInviteCreate,
    CollaboratorInviteRequest,
    FolderCreate,
)
from app.schemas.users import UserCreate, UserSelfUpdate
from app.types import CollaboratorRole, Scope


def make_user(db, email, first_name):
    return db.users.create_user(
        UserCreate(email=email, first_name=first_name, password="pw", invite_token=None)
    )


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield InterviewDataBase(session)


@pytest.fixture
def owner(db):
    return make_user(db, "owner@example.com", "Olive")


@pytest.fixture
def member(db):
    return make_user(db, "member@example.com", "Mads")


@pytest.fixture
def folder(db, owner):
    return db.projects.create_folder("Fieldwork", owner.id)


@pytest.fixture
def sent(monkeypatch) -> list[dict]:
    outbox: list[dict] = []

    async def fake_send_email(recipient, subject, **kwargs):
        outbox.append({"recipient": recipient, "subject": subject, **kwargs})

    monkeypatch.setattr(folders_api, "send_email", fake_send_email)
    return outbox


def token_of(email: dict) -> str:
    """The token in an invite email's accept link."""
    html = email["html_content"]
    start = html.index("/dashboard/invites/accept?token=")
    link = html[start : html.index('"', start)]
    return parse_qs(urlparse(link).query)["token"][0]


def jwt(user) -> AuthToken:
    return AuthToken(user_id=user.id, scope=Scope.USER)


def invite(db, folder, by, email, role=CollaboratorRole.EDITOR):
    return asyncio.run(
        folders_api.create_collaborator_invite(
            folder.id,
            CollaboratorInviteCreate(email=email, role=role),
            db,
            jwt(by),
            jwt(by),
        )
    )


def accept(db, user, token):
    return asyncio.run(
        folders_api.accept_collaborator_invite(
            CollaboratorInviteRequest(token=token), db, jwt(user)
        )
    )


@pytest.mark.usefixtures("member", "sent")
def test_the_inviter_cannot_tell_an_account_from_an_unknown_email(db, folder, owner):
    known = invite(db, folder, owner, "member@example.com")
    unknown = invite(db, folder, owner, "nobody@example.com")

    fields = set(known.model_dump()) - {"id", "email", "created_at", "expires_at"}
    assert {f: getattr(known, f) for f in fields} == {
        f: getattr(unknown, f) for f in fields
    }
    assert [i.email for i in db.projects.get_collaborator_invites(folder.id)] == [
        "member@example.com",
        "nobody@example.com",
    ]
    # Neither is a collaborator until they accept.
    assert [c.user.id for c in db.projects.get_collaborators(folder.id)] == [owner.id]


def test_only_the_recipients_email_differs(db, folder, owner, member, sent):
    invite(db, folder, owner, "member@example.com")
    invite(db, folder, owner, "nobody@example.com")

    to_member, to_nobody = sent
    # The subject is fixed: the inviter's name is text they chose.
    assert to_member["subject"] == to_nobody["subject"]
    assert "Olive" not in to_member["subject"]
    assert "Olive (owner@example.com)" in to_member["html_content"]
    assert "Hello Mads" in to_member["html_content"]
    assert "/login?request" not in to_member["html_content"]
    assert "Hello there" in to_nobody["html_content"]
    assert "/login?request" in to_nobody["html_content"]


def test_accepting_makes_a_collaborator_with_the_invited_role(
    db, folder, owner, member, sent
):
    invite(db, folder, owner, "member@example.com", CollaboratorRole.ANNOTATOR)

    accept(db, member, token_of(sent[0]))

    roles = {c.user.id: c.role for c in db.projects.get_collaborators(folder.id)}
    assert roles[member.id] is CollaboratorRole.ANNOTATOR
    assert db.projects.get_collaborator_invites(folder.id) == []


def test_someone_who_signs_up_later_accepts_the_same_link(db, folder, owner, sent):
    invite(db, folder, owner, "Late@Example.com")
    late = make_user(db, "late@example.com", "Lars")

    accept(db, late, token_of(sent[0]))

    assert late.id in {c.user.id for c in db.projects.get_collaborators(folder.id)}


def test_a_link_is_spent_once_accepted(db, folder, owner, member, sent):
    invite(db, folder, owner, "member@example.com")
    token = token_of(sent[0])
    accept(db, member, token)

    with pytest.raises(CollaboratorInviteInvalidError):
        accept(db, member, token)


def test_reinviting_replaces_the_link_and_the_role(db, folder, owner, member, sent):
    invite(db, folder, owner, "member@example.com", CollaboratorRole.VIEWER)
    invite(db, folder, owner, "member@example.com", CollaboratorRole.EDITOR)
    old, new = token_of(sent[0]), token_of(sent[1])

    assert len(db.projects.get_collaborator_invites(folder.id)) == 1
    with pytest.raises(CollaboratorInviteInvalidError):
        accept(db, member, old)
    accept(db, member, new)
    roles = {c.user.id: c.role for c in db.projects.get_collaborators(folder.id)}
    assert roles[member.id] is CollaboratorRole.EDITOR


def test_an_expired_link_is_refused(db, folder, owner, member, sent):
    invite(db, folder, owner, "member@example.com")
    db.session.execute(
        update(CollaboratorInviteTable).values(expires_at=now() - timedelta(minutes=1))
    )
    db.session.commit()

    with pytest.raises(CollaboratorInviteExpiredError):
        accept(db, member, token_of(sent[0]))


def test_a_revoked_link_is_refused(db, folder, owner, member, sent):
    pending = invite(db, folder, owner, "member@example.com")
    asyncio.run(
        folders_api.delete_collaborator_invite(
            folder.id, pending.id, db, jwt(owner), jwt(owner)
        )
    )

    with pytest.raises(CollaboratorInviteInvalidError):
        accept(db, member, token_of(sent[0]))


def test_revoking_is_scoped_to_the_folder(db, folder, owner, member, sent):
    pending = invite(db, folder, owner, "member@example.com")
    other = db.projects.create_folder("Elsewhere", owner.id)

    db.projects.delete_collaborator_invite(other.id, pending.id, CollaboratorRole.ADMIN)

    assert len(db.projects.get_collaborator_invites(folder.id)) == 1


def test_inviting_an_existing_collaborator_is_a_conflict(db, folder, owner, sent):
    with pytest.raises(AlreadyCollaboratorError):
        invite(db, folder, owner, "OWNER@example.com")
    assert sent == []


def test_a_failed_email_leaves_no_invite(db, folder, owner, monkeypatch):
    async def failing_send_email(*args, **kwargs):
        raise OSError("SMTP down")

    monkeypatch.setattr(folders_api, "send_email", failing_send_email)

    with pytest.raises(EmailSendError):
        invite(db, folder, owner, "nobody@example.com")
    assert db.projects.get_collaborator_invites(folder.id) == []


def test_creating_a_folder_invites_each_listed_person_with_their_own_role(
    db, owner, member, sent
):
    """`create_folder` used to zip the users it found against the requested
    collaborators, so roles landed on whoever the database returned first and
    an unknown email shifted everyone after it."""
    body = FolderCreate.model_validate(
        {
            "title": "Shared",
            "folder_collaborations": [
                {"email": "nobody@example.com", "role": "admin"},
                {"email": "member@example.com", "role": "viewer"},
                {"email": "owner@example.com", "role": "viewer"},
                {"email": "Member@example.com", "role": "editor"},
            ],
        }
    )
    created = asyncio.run(folders_api.create_folder(body, db, jwt(owner)))

    roles = {i.email: i.role for i in db.projects.get_collaborator_invites(created.id)}
    assert roles == {
        "nobody@example.com": CollaboratorRole.ADMIN,
        "member@example.com": CollaboratorRole.VIEWER,
    }
    assert sorted(e["recipient"] for e in sent) == [
        "member@example.com",
        "nobody@example.com",
    ]
    assert [c.user.id for c in db.projects.get_collaborators(created.id)] == [owner.id]


def test_declining_spends_the_link(db, folder, owner, member, sent):
    invite(db, folder, owner, "member@example.com")
    token = token_of(sent[0])

    asyncio.run(
        folders_api.decline_collaborator_invite(
            CollaboratorInviteRequest(token=token), db, jwt(member)
        )
    )

    assert db.projects.get_collaborator_invites(folder.id) == []
    with pytest.raises(CollaboratorInviteInvalidError):
        accept(db, member, token)


def test_inspect_shows_what_the_invite_offers(db, folder, owner, member, sent):
    invite(db, folder, owner, "member@example.com", CollaboratorRole.VIEWER)

    details = asyncio.run(
        folders_api.inspect_collaborator_invite(
            CollaboratorInviteRequest(token=token_of(sent[0])), db, jwt(member)
        )
    )

    assert details.folder_title == "Fieldwork"
    assert details.role is CollaboratorRole.VIEWER
    assert details.invited_by_name == "Olive"


def test_deleting_the_folder_takes_its_invites(db, folder, owner, sent):
    invite(db, folder, owner, "nobody@example.com")

    db.projects.delete_folder(folder.id)

    assert db.session.query(CollaboratorInviteTable).count() == 0


def test_a_name_that_reaches_invite_emails_is_bounded():
    with pytest.raises(ValidationError):
        UserSelfUpdate(first_name="x" * 101)
    with pytest.raises(ValidationError):
        UserSelfUpdate(first_name="   ")
    assert UserSelfUpdate(first_name="  Olive ").first_name == "Olive"
