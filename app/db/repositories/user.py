import datetime
from collections.abc import Sequence
from datetime import timedelta
from typing import Literal

from pydantic import UUID4
from sqlalchemy import Update, delete, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from ainterviewer.settings import settings as lib_settings
from ainterviewer.utils import now

from ...auth import get_password_hash
from ...services.email.mail import email_templates, send_email
from ...settings import DemoLimits, app_settings
from ...types import Scope
from ..models import (
    UNSET,
    AccessRequestCreate,
    AccessRequestPublic,
    InvitationPublic,
    InvitationUpdate,
    UserAdmin,
    UserAdminUpdate,
    UserCreate,
    UserPrivate,
    UserPublic,
    UserSelfUpdate,
)
from ..tables import AccessRequestTable, InvitationTable, UserActivityTable, UserTable
from ..types import AccessRequestStatus
from .base import BaseRepository
from .errors import InvitationAliasTakenError


class UserRepository(BaseRepository):
    """Repository for User, AccessRequest, and Invitation operations."""

    # ==================== User Methods ====================

    def create_user(self, user: UserCreate) -> UserPublic:
        user.password = get_password_hash(user.password)

        new_user = UserTable(**user.model_dump())
        self.session.add(new_user)
        self.session.commit()
        self.session.refresh(new_user)

        return UserPublic.model_validate(new_user)

    def delete_user(self, id: UUID4):
        # ORM-level delete so relationship cascades fire (owned projects with
        # their interviews/messages, annotations, experiments, refresh tokens);
        # a bulk DELETE would hit FK constraints on dependent rows.
        user = self.session.get(UserTable, id)
        if user is None:
            return
        self.session.delete(user)
        self.session.commit()

    def update_user(self, user_id: UUID4, data: UserSelfUpdate) -> UserPublic:
        values = {k: v for k, v in data.model_dump().items() if v is not UNSET}

        if values:
            statement = (
                update(UserTable).where(UserTable.id == user_id).values(**values)
            )
            self.session.execute(statement)
            self.session.commit()

        return self.get_user_by_id(user_id)

    def update_user_email(self, user_id: UUID4, email: str) -> UserPublic:
        statement = update(UserTable).where(UserTable.id == user_id).values(email=email)
        self.session.execute(statement)
        self.session.commit()

        return self.get_user_by_id(user_id)

    def update_user_password(self, user_id: UUID4, new_password: str) -> None:
        statement = (
            update(UserTable)
            .where(UserTable.id == user_id)
            .values(password=get_password_hash(new_password))
        )
        self.session.execute(statement)
        self.session.commit()

    def set_email_verified(self, user_id: UUID4, verified: bool = True) -> None:
        statement = (
            update(UserTable)
            .where(UserTable.id == user_id)
            .values(email_verified=verified)
        )
        self.session.execute(statement)
        self.session.commit()

    def update_user_status(
        self,
        id: UUID4,
        last_login: bool = False,
        last_active: bool = True,
    ):
        values = {}
        if last_login:
            values["last_login"] = now()
        if last_active:
            values["last_active"] = now()

        if not values:
            return

        statement = update(UserTable).where(UserTable.id == id).values(**values)
        self.session.execute(statement)
        self.session.commit()

    def record_activity(self, id: UUID4, at: datetime.datetime) -> None:
        """Mark ``id`` active on ``at``'s day and move ``last_active`` to ``at``.

        Upserts the day's `user_activity` row, keeping its ``first_seen``.
        Debouncing is the caller's job (`_record_activity`,
        app/dependencies.py); this always writes.
        """
        statement = sqlite_insert(UserActivityTable).values(
            user_id=id, day=at.date(), first_seen=at, last_seen=at
        )
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[UserActivityTable.user_id, UserActivityTable.day],
                set_={"last_seen": statement.excluded.last_seen},
            )
        )
        self.session.execute(
            update(UserTable)
            .where(UserTable.id == id)
            .values(last_active=at)
            .execution_options(synchronize_session=False)
        )
        self.session.commit()

    def get_demo_limits(self, user_id: UUID4) -> DemoLimits:
        """The configured demo budgets with this user's overrides applied.

        A NULL override, or a user that does not exist, falls back to the
        configured default (`app.demo_limits`).
        """
        defaults = app_settings.app.demo_limits
        user = self.session.get(UserTable, user_id)
        if user is None:
            return defaults

        overrides = {
            "max_concurrent_synthetic_interviews": user.demo_concurrent_limit,
            "daily_synthetic_interviews": user.demo_daily_limit,
            "lifetime_synthetic_interviews": user.demo_lifetime_limit,
        }
        return defaults.model_copy(
            update={k: v for k, v in overrides.items() if v is not None}
        )

    def get_user_private(
        self,
        email: str | None = None,
        user_id: UUID4 | None = None,
    ) -> UserPrivate:
        statement = select(UserTable).where(
            or_(UserTable.email == email, UserTable.id == user_id)
        )
        user = self.session.execute(statement).scalar_one()

        return UserPrivate.model_validate(user)

    def get_user_by_id(self, user_id: UUID4) -> UserPublic:
        statement = select(UserTable).where(UserTable.id == user_id)
        user = self.session.execute(statement).scalar_one()
        return UserPublic.model_validate(user)

    def get_users(
        self,
    ) -> list[UserPublic]:
        statement = select(UserTable)
        users = self.session.execute(statement).scalars().all()

        return [UserPublic.model_validate(user) for user in users]

    def get_users_admin(self) -> list[UserAdmin]:
        statement = select(UserTable)
        users = self.session.execute(statement).scalars().all()
        return [UserAdmin.model_validate(user) for user in users]

    def update_user_admin(self, user_id: UUID4, data: UserAdminUpdate) -> UserAdmin:
        values = {k: v for k, v in data.model_dump().items() if v is not UNSET}

        if values:
            statement = (
                update(UserTable).where(UserTable.id == user_id).values(**values)
            )
            self.session.execute(statement)
            self.session.commit()

        user = self.session.execute(
            select(UserTable).where(UserTable.id == user_id)
        ).scalar_one()
        return UserAdmin.model_validate(user)

    def update_admin_note(self, user_id: UUID4, note: str | None) -> UserAdmin:
        statement = (
            update(UserTable)
            .where(UserTable.id == user_id)
            .values(admin_note=note, admin_note_updated_at=now())
        )
        self.session.execute(statement)
        self.session.commit()

        user = self.session.execute(
            select(UserTable).where(UserTable.id == user_id)
        ).scalar_one()
        return UserAdmin.model_validate(user)

    # ==================== Access Request Methods ====================

    def create_access_request(self, access_request: AccessRequestCreate):
        request = AccessRequestTable(**access_request.model_dump())
        self.session.add(request)
        self.session.commit()

    def get_access_request(self, access_request_id: UUID4) -> AccessRequestPublic:
        statement = select(AccessRequestTable).where(
            AccessRequestTable.id == access_request_id
        )
        request = self.session.execute(statement).scalar_one()
        return AccessRequestPublic.model_validate(request)

    def get_access_requests(self) -> Sequence[AccessRequestPublic]:
        statement = select(AccessRequestTable)
        requests = self.session.execute(statement).scalars().all()

        return [AccessRequestPublic.model_validate(request) for request in requests]

    def delete_access_requests(self, ids: list[UUID4]) -> None:
        """Delete access requests, detaching any invitation issued for them.

        An invitation outlives the request it came from -- pruning the request
        list must not revoke a pending invite -- so the link is cleared and the
        invitation kept. The foreign key declares SET NULL for the same reason;
        this does it explicitly so the behaviour also holds where foreign-key
        enforcement is switched off.
        """
        self.session.execute(
            update(InvitationTable)
            .where(InvitationTable.access_request_id.in_(ids))
            .values(access_request_id=None)
        )
        self.session.execute(
            delete(AccessRequestTable).where(AccessRequestTable.id.in_(ids))
        )
        self.session.commit()

    async def process_access_request(
        self,
        access_request_id: UUID4,
        scope: Scope,
        action: Literal["approve", "deny"],
        approver_id: UUID4,
    ):
        statement = select(AccessRequestTable).where(
            AccessRequestTable.id == access_request_id
        )
        access_request = self.session.execute(statement).scalar_one()

        if action == "approve":
            # WARNING: Invites are added to the DB, then the email is send,
            # and if the email fails, the invite will be deleted. This has
            # the potential to create a race condition where the invite is
            # created but the email has not been send to the user.
            invite = self.create_invitation(
                access_request.email,
                access_request_id=access_request.id,
                user_scope=scope,
            )

            try:
                await send_email(
                    access_request.email,
                    "Access Approved",
                    html_content=email_templates.get_template("invite.jinja").render(
                        recipient_name=access_request.name,
                        invite_link=invite.invitation_link,
                    ),
                )
                access_request.status = AccessRequestStatus.FULFILLED
            except Exception:
                self.delete_invitation(invite.id)
                raise

        elif action == "deny":
            access_request.status = AccessRequestStatus.DENIED
            await send_email(
                access_request.email,
                "Access Denied",
                body="Your access request has been denied.",
            )

        access_request.processed_by_id = approver_id

        self.session.add(access_request)
        self.session.commit()

    # ==================== Invitation Methods ====================

    def create_invitation(
        self,
        email: str | None = None,
        expires_at: datetime.datetime | None = None,
        access_request_id: UUID4 | None = None,
        reuseable: bool = False,
        user_scope: Scope = Scope.USER,
        user_expires: datetime.datetime | None = None,
        title: str | None = None,
        alias: str | None = None,
        user_demo_concurrent_limit: int | None = None,
        user_demo_daily_limit: int | None = None,
        user_demo_lifetime_limit: int | None = None,
    ) -> InvitationPublic:
        if expires_at is None:
            expires_at = now() + timedelta(days=1)

        invitation = InvitationTable(
            email=email,
            expires_at=expires_at,
            access_request_id=access_request_id,
            reuseable=reuseable,
            user_scope=user_scope,
            user_expires=user_expires,
            title=title,
            alias=alias,
            user_demo_concurrent_limit=user_demo_concurrent_limit,
            user_demo_daily_limit=user_demo_daily_limit,
            user_demo_lifetime_limit=user_demo_lifetime_limit,
        )

        self.session.add(invitation)
        self._commit_invitation(alias, None)
        self.session.refresh(invitation)

        return InvitationPublic.model_validate(invitation)

    def update_invitation(
        self,
        id: UUID4,
        data: InvitationUpdate,
    ) -> InvitationPublic:
        values = {k: v for k, v in data.model_dump().items() if v is not UNSET}

        if not values:
            return self.check_invite_token(id)

        statement = (
            update(InvitationTable).where(InvitationTable.id == id).values(**values)
        )

        self._commit_invitation(values.get("alias"), id, statement)

        return self.check_invite_token(id)

    def _commit_invitation(
        self, alias: str | None, id: UUID4 | None, statement: Update | None = None
    ) -> None:
        """Run `statement`, if any, and commit, turning a clash on `alias`
        into `InvitationAliasTakenError`. Any other integrity failure (the
        email, say) is re-raised as is.

        `id` is the invitation being updated, None for one being created.
        """
        try:
            if statement is not None:
                self.session.execute(statement)
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            if alias is None:
                raise
            holder = select(InvitationTable.id).where(InvitationTable.alias == alias)
            if id is not None:
                holder = holder.where(InvitationTable.id != id)
            if self.session.scalar(holder):
                raise InvitationAliasTakenError(alias) from None
            raise

    def check_invite_token(self, token: UUID4 | str) -> InvitationPublic:
        """The invitation `token` names: its id, or its alias as a string.

        Raises NoResultFound when there is none.
        """
        if isinstance(token, str):
            condition = InvitationTable.alias == token.strip().lower()
        else:
            condition = InvitationTable.id == token
        statement = select(InvitationTable).where(condition)
        invitation = self.session.execute(statement).scalar_one()

        if invitation.expires_at:
            invitation.expires_at = invitation.expires_at.replace(
                tzinfo=lib_settings.tzinfo
            )

        return InvitationPublic.model_validate(invitation)

    def delete_invitation(self, id: UUID4) -> bool:
        """Delete an invitation. Returns True if a row was actually deleted."""
        statement = delete(InvitationTable).where(InvitationTable.id == id)
        result = self.session.execute(statement)
        self.session.commit()
        return result.rowcount > 0  # ty:ignore[unresolved-attribute]

    def delete_invitations(self, ids: list[UUID4]):
        statement = delete(InvitationTable).where(InvitationTable.id.in_(ids))
        self.session.execute(statement)
        self.session.commit()

    def get_invitations(self, reuseable=False) -> list[InvitationPublic]:
        statement = select(InvitationTable)

        if reuseable:
            statement = statement.where(InvitationTable.reuseable)

        invitations = self.session.execute(statement).scalars().all()

        return [
            InvitationPublic.model_validate(invitation) for invitation in invitations
        ]
