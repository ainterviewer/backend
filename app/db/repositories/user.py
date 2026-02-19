from collections.abc import Sequence
from datetime import timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import UUID4
from sqlalchemy import delete, or_, select, update

from ainterviewer.utils import now

from ...auth import get_password_hash
from ...services.email.mail import email_templates, send_email
from ..models import (
    AccessRequestCreate,
    AccessRequestPublic,
    InvitationPublic,
    UserCreate,
    UserPrivate,
    UserPublic,
)
from ..tables import AccessRequestTable, InvitationTable, UserTable
from ..types import AccessRequestStatus
from .base import BaseRepository


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
        statement = delete(UserTable).where(UserTable.id == id)
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

    # ==================== Access Request Methods ====================

    def create_access_request(self, access_request: AccessRequestCreate):
        request = AccessRequestTable(**access_request.model_dump())
        self.session.add(request)
        self.session.commit()

    def get_access_requests(self) -> Sequence[AccessRequestPublic]:
        statement = select(AccessRequestTable)
        requests = self.session.execute(statement).scalars().all()

        return [AccessRequestPublic.model_validate(request) for request in requests]

    def delete_access_requests(self, ids: list[UUID4]) -> None:
        statement = delete(AccessRequestTable).where(AccessRequestTable.id.in_(ids))
        self.session.execute(statement)
        self.session.commit()

    async def process_access_request(
        self,
        access_request_id: UUID4,
        action: Literal["approve", "deny"],
        approver_id: UUID4,
    ):
        statement = select(AccessRequestTable).where(
            AccessRequestTable.id == access_request_id
        )
        access_request = self.session.execute(statement).scalar_one()

        if action == "approve":
            access_request.status = AccessRequestStatus.FULFILLED
            # WARNING: Invites are added to the DB, then the email is send,
            # and if the email fails, the invite will be deleted. This has
            # the potential to create a race condition where the invite is
            # created but the email has not been send to the user.
            invite = self.create_invitation(
                access_request.email, access_request_id=access_request.id
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
            except Exception as e:
                self.delete_invitation(invite.id)
                raise e

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
        email: str,
        access_request_id: UUID4 | None = None,
    ) -> InvitationPublic:
        expires_at = now() + timedelta(days=1)
        invitation = InvitationTable(
            email=email,
            expires_at=expires_at,
            access_request_id=access_request_id,
        )
        self.session.add(invitation)
        self.session.commit()
        self.session.refresh(invitation)

        return InvitationPublic.model_validate(invitation)

    def check_invite_token(self, token: UUID4) -> InvitationPublic:
        statement = select(InvitationTable).where(InvitationTable.id == token)
        invitation = self.session.execute(statement).scalar_one()

        invitation.expires_at = invitation.expires_at.replace(
            tzinfo=ZoneInfo("Europe/Copenhagen")
        )

        return InvitationPublic.model_validate(invitation)

    def delete_invitation(self, id: UUID4):
        statement = delete(InvitationTable).where(InvitationTable.id == id)
        self.session.execute(statement)
        self.session.commit()
