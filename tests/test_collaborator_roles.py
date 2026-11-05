"""Nobody manages a folder's collaborators above their own role, and a folder
always keeps an admin.

Every collaborator route was gated by `FolderEditor` alone, so an editor could
make themselves or anyone else an admin, demote or remove the folder's admins,
and leave it with none. Now an editor manages viewers, annotators and editors;
only admins touch admins, and the last admin cannot go.

Runs against an in-memory SQLite database with email sending stubbed out.
"""

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.dashboard import folders as folders_api
from app.auth import AuthToken
from app.db.crud import InterviewDataBase
from app.db.repositories.errors import LastAdminError, RoleAboveOwnError
from app.db.tables import Base, CollaboratorTable
from app.schemas.projects import CollaboratorInviteCreate
from app.schemas.users import UserCreate
from app.types import CollaboratorRole, Scope

ADMIN = CollaboratorRole.ADMIN
EDITOR = CollaboratorRole.EDITOR
VIEWER = CollaboratorRole.VIEWER


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield InterviewDataBase(session)


@pytest.fixture(autouse=True)
def sent(monkeypatch) -> list[dict]:
    outbox: list[dict] = []

    async def fake_send_email(recipient, subject, **kwargs):
        outbox.append({"recipient": recipient, "subject": subject, **kwargs})

    monkeypatch.setattr(folders_api, "send_email", fake_send_email)
    return outbox


def make_user(db, name, scope=Scope.USER):
    user = db.users.create_user(
        UserCreate(
            email=f"{name}@example.com",
            first_name=name.title(),
            password="pw",
            invite_token=None,
        )
    )
    return AuthToken(user_id=user.id, scope=scope)


@pytest.fixture
def owner(db):
    return make_user(db, "owner")


@pytest.fixture
def folder(db, owner):
    return db.projects.create_folder("Fieldwork", owner.user_id)


def join(db, folder, token, role):
    db.session.add(
        CollaboratorTable(
            folder_id=folder.id, user_id=token.user_id, role=role, added_by_id=None
        )
    )
    db.session.commit()
    return token


@pytest.fixture
def editor(db, folder):
    return join(db, folder, make_user(db, "editor"), EDITOR)


def role_of(db, folder, token):
    return db.projects.get_user_role_on_folder(token.user_id, folder.id)


def set_role(db, folder, by, target, role):
    return asyncio.run(
        folders_api.update_collaborator_role(
            folder.id, target.user_id, role, db, by, by
        )
    )


def remove(db, folder, by, target):
    return asyncio.run(
        folders_api.remove_collaborator(folder.id, target.user_id, db, by, by)
    )


def invite(db, folder, by, email, role):
    return asyncio.run(
        folders_api.create_collaborator_invite(
            folder.id, CollaboratorInviteCreate(email=email, role=role), db, by, by
        )
    )


def test_an_editor_cannot_invite_an_admin(db, folder, editor, sent):
    with pytest.raises(RoleAboveOwnError):
        invite(db, folder, editor, "new@example.com", ADMIN)
    assert sent == []
    assert db.projects.get_collaborator_invites(folder.id) == []


def test_an_editor_invites_up_to_their_own_role(db, folder, editor):
    assert invite(db, folder, editor, "new@example.com", EDITOR).role is EDITOR


def test_an_editor_cannot_promote_anyone_to_admin(db, folder, editor):
    viewer = join(db, folder, make_user(db, "viewer"), VIEWER)

    with pytest.raises(RoleAboveOwnError):
        set_role(db, folder, editor, viewer, ADMIN)
    with pytest.raises(RoleAboveOwnError):
        set_role(db, folder, editor, editor, ADMIN)
    assert role_of(db, folder, viewer) is VIEWER
    assert role_of(db, folder, editor) is EDITOR


def test_an_editor_cannot_demote_or_remove_an_admin(db, folder, owner, editor):
    with pytest.raises(RoleAboveOwnError):
        set_role(db, folder, editor, owner, VIEWER)
    with pytest.raises(RoleAboveOwnError):
        remove(db, folder, editor, owner)
    assert role_of(db, folder, owner) is ADMIN


def test_an_editor_manages_those_at_or_below_their_role(db, folder, editor):
    viewer = join(db, folder, make_user(db, "viewer"), VIEWER)
    other = join(db, folder, make_user(db, "other"), EDITOR)

    set_role(db, folder, editor, viewer, EDITOR)
    remove(db, folder, editor, other)

    assert role_of(db, folder, viewer) is EDITOR
    assert role_of(db, folder, other) is None


def test_an_editor_cannot_revoke_or_resend_an_admin_invite(db, folder, owner, editor):
    pending = invite(db, folder, owner, "boss@example.com", ADMIN)

    with pytest.raises(RoleAboveOwnError):
        asyncio.run(
            folders_api.delete_collaborator_invite(
                folder.id, pending.id, db, editor, editor
            )
        )
    with pytest.raises(RoleAboveOwnError):
        invite(db, folder, editor, "boss@example.com", VIEWER)
    assert [i.role for i in db.projects.get_collaborator_invites(folder.id)] == [ADMIN]


def test_an_admin_manages_admins(db, folder, owner, editor):
    set_role(db, folder, owner, editor, ADMIN)
    set_role(db, folder, owner, editor, VIEWER)

    assert role_of(db, folder, editor) is VIEWER


def test_the_last_admin_can_be_neither_demoted_nor_removed(db, folder, owner):
    with pytest.raises(LastAdminError):
        set_role(db, folder, owner, owner, EDITOR)
    with pytest.raises(LastAdminError):
        remove(db, folder, owner, owner)
    assert role_of(db, folder, owner) is ADMIN


def test_an_admin_may_step_down_while_another_remains(db, folder, owner):
    join(db, folder, make_user(db, "second"), ADMIN)

    remove(db, folder, owner, owner)

    assert role_of(db, folder, owner) is None


def test_a_platform_admin_counts_as_a_folder_admin(db, folder, editor):
    staff = make_user(db, "staff", scope=Scope.ADMIN)

    set_role(db, folder, staff, editor, ADMIN)

    assert role_of(db, folder, editor) is ADMIN


def leave(db, folder, token):
    return asyncio.run(folders_api.leave_folder(folder.id, db, token, token))


def test_any_collaborator_may_leave(db, folder):
    viewer = join(db, folder, make_user(db, "viewer"), VIEWER)

    leave(db, folder, viewer)

    assert role_of(db, folder, viewer) is None


def test_the_last_admin_cannot_leave(db, folder, owner):
    with pytest.raises(LastAdminError):
        leave(db, folder, owner)
    assert role_of(db, folder, owner) is ADMIN


def test_an_admin_may_leave_while_another_remains(db, folder, owner):
    join(db, folder, make_user(db, "second"), ADMIN)

    leave(db, folder, owner)

    assert role_of(db, folder, owner) is None
