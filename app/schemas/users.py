"""Accounts, sign-in, invitations and access requests."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import (
    MISSING,
    UUID4,
    AfterValidator,
    EmailStr,
    Field,
    NonNegativeInt,
    computed_field,
)

from ainterviewer.types import (
    TimeDelta,
)
from ainterviewer.utils import now

from ..db._extra import CustomEmailStr
from ..db.types import (
    AccessRequestStatus,
)
from ..settings import app_settings
from ..types import (
    Scope,
)
from .common import Schema


class AccessRequestBase(Schema):
    name: str
    email: CustomEmailStr
    organization: str | None = None
    message: str | None = None


class AccessRequestCreate(AccessRequestBase): ...


class AccessRequestPublic(AccessRequestBase):
    id: UUID4
    created_at: datetime
    updated_at: datetime
    status: AccessRequestStatus
    processed_by_id: UUID4 | None


_INVITATION_ALIAS = re.compile(r"[a-z0-9][a-z0-9_-]{1,63}")


def normalize_invitation_alias(value: str) -> str:
    """Lowercase an invitation alias and check it can stand in for the id.

    Registration parses `invite_token` as a UUID first, so an alias that reads
    as one -- dashed or as 32 bare hex digits -- would never be looked up as an
    alias.
    """
    alias = value.strip().lower()
    if not _INVITATION_ALIAS.fullmatch(alias):
        raise ValueError(
            "An alias is 2-64 letters, digits, '-' or '_', starting with a "
            "letter or digit"
        )
    try:
        UUID(alias)
    except ValueError:
        return alias
    raise ValueError("An alias cannot be a UUID")


InvitationAlias = Annotated[str, AfterValidator(normalize_invitation_alias)]


class InvitationBase(Schema):
    expires_at: datetime | None = None
    reuseable: bool = False
    user_scope: Scope = Scope.USER
    user_expires: datetime | TimeDelta | None = None
    title: str | None = None
    alias: InvitationAlias | None = None
    access_request_id: UUID4 | None = None
    # Demo budgets handed to the user who registers with this invitation;
    # None leaves them on the configured default.
    user_demo_concurrent_limit: NonNegativeInt | None = None
    user_demo_daily_limit: NonNegativeInt | None = None
    user_demo_lifetime_limit: NonNegativeInt | None = None


class InvitationCreate(InvitationBase): ...


class InvitationUpdate(Schema):
    email: str | None | MISSING = MISSING
    expires_at: datetime | None | MISSING = MISSING
    reuseable: bool | MISSING = MISSING
    user_scope: Scope | MISSING = MISSING
    user_expires: datetime | TimeDelta | None | MISSING = MISSING
    title: str | None | MISSING = MISSING
    alias: InvitationAlias | None | MISSING = MISSING
    user_demo_concurrent_limit: NonNegativeInt | None | MISSING = MISSING
    user_demo_daily_limit: NonNegativeInt | None | MISSING = MISSING
    user_demo_lifetime_limit: NonNegativeInt | None | MISSING = MISSING


class InvitationPublic(InvitationBase):
    id: UUID4
    email: str | None

    @computed_field()
    def invitation_link(self) -> str:
        token = self.alias or self.id
        return f"{app_settings.sveltekit_platform_public_addr}/sign-up?token={token}"


class UserBase(Schema):
    email: EmailStr
    first_name: str
    last_name: str | None = None
    created_at: datetime
    last_active: datetime
    last_login: datetime | None = None
    scope: Scope = Scope.USER


def _within_bcrypt_limit(value: str) -> str:
    # bcrypt silently truncates input beyond 72 bytes (not chars), so reject
    # anything longer to avoid surprising password equivalence.
    if len(value.encode("utf-8")) > 72:
        raise ValueError("Password must be at most 72 bytes")
    return value


#: A password as a client sets one.
NewPassword = Annotated[str, Field(min_length=8), AfterValidator(_within_bcrypt_limit)]


class UserCreateRequest(Schema):
    """API request model for user registration.

    Only what the person signing up chooses. The scope, the timestamps and the
    invitation's snapshot are the server's, so there is no field for a client
    to send them in.
    """

    email: EmailStr
    first_name: str
    last_name: str | None = None
    invite_token: UUID4 | str | None = Field(union_mode="left_to_right")
    research_consent: bool = False
    password: NewPassword


class UserCreate(UserBase):
    """Internal model for creating a user, includes snapshot fields."""

    invite_token: UUID4 | str | None = Field(union_mode="left_to_right")
    created_at: datetime = Field(default_factory=now)
    last_active: datetime = Field(default_factory=now)
    research_consent: bool = False
    password: str

    registration_token: str | None = None
    invitation_title: str | None = None
    expires_at: datetime | None = None
    access_request_message: str | None = None
    organization: str | None = None
    demo_concurrent_limit: int | None = None
    demo_daily_limit: int | None = None
    demo_lifetime_limit: int | None = None


class UserPrivate(UserBase):
    id: UUID4
    password: str
    with_preview_features: bool
    organization: str | None = None
    email_verified: bool = False
    two_factor_enabled: bool = True


class UserPublic(UserBase):
    id: UUID4
    invitation_title: str | None = None
    expires_at: datetime | None = None
    with_preview_features: bool
    organization: str | None = None


class UserAdmin(UserPublic):
    access_request_message: str | None = None
    admin_note: str | None = None
    admin_note_updated_at: datetime | None = None
    two_factor_enabled: bool = True
    # Overrides of the demo budgets; None means the configured default.
    demo_concurrent_limit: int | None = None
    demo_daily_limit: int | None = None
    demo_lifetime_limit: int | None = None


class UserAdminUpdate(Schema):
    scope: Scope | MISSING = MISSING
    with_preview_features: bool | MISSING = MISSING
    organization: str | None | MISSING = MISSING
    expires_at: datetime | None | MISSING = MISSING
    two_factor_enabled: bool | MISSING = MISSING
    # None puts the user back on the configured default.
    demo_concurrent_limit: NonNegativeInt | None | MISSING = MISSING
    demo_daily_limit: NonNegativeInt | None | MISSING = MISSING
    demo_lifetime_limit: NonNegativeInt | None | MISSING = MISSING


class UserSelfUpdate(Schema):
    """Profile fields a user may change on their own account."""

    first_name: str | MISSING = MISSING
    last_name: str | None | MISSING = MISSING
    organization: str | None | MISSING = MISSING


class AuthorPublic(Schema):
    """Who wrote a coding or a comment.

    Codings and comments are author specific, so every one of them is shown
    with a name attached. Carrying the author inline saves the client from
    resolving user ids against a separate collaborator listing.
    """

    id: UUID4
    first_name: str
    last_name: str | None = None
    email: EmailStr


class LoginData(Schema):
    email: str
    password: str
    extended: bool = False


class VerifyEmailRequest(Schema):
    token: str


class ResendVerificationRequest(Schema):
    email: EmailStr


class VerifyLoginCodeRequest(Schema):
    email: str
    code: str
    extended: bool = False


class UpdateEmailRequest(Schema):
    password: str
    new_email: EmailStr


class _NewPasswordRequest(Schema):
    new_password: NewPassword


class UpdatePasswordRequest(_NewPasswordRequest):
    current_password: str


class ForgotPasswordRequest(Schema):
    email: EmailStr


class ResetPasswordRequest(_NewPasswordRequest):
    token: str


class DeleteAccountRequest(Schema):
    password: str


class DemoLimitDefaults(Schema):
    """The configured demo budgets (`app.demo_limits`): what a user without an
    override gets."""

    concurrent: int
    daily: int
    lifetime: int


class DemoBudgetItem(Schema):
    limit: int
    used: int

    @computed_field
    @property
    def remaining(self) -> int:
        return max(self.limit - self.used, 0)


class DemoBudget(Schema):
    """A demo user's synthetic-interview budgets and how much of each is spent.

    `limit` is the user's own (an override, else the configured default), so
    two demo users can see different numbers.
    """

    #: Interviews in flight at once.
    concurrent: DemoBudgetItem
    #: Interviews started in the last 24 hours.
    daily: DemoBudgetItem
    #: Interviews started ever.
    lifetime: DemoBudgetItem


class UserNotifications(Schema):
    """Counts for the badges in the account menu.

    One field per queue rather than one number and a label. A platform admin
    who also collaborates on projects has work waiting in *both*, and the two
    are different jobs -- rewording a question in your own guide is not
    reviewing somebody else's for safety. Counting only one of them hid the
    other completely.
    """

    #: Still-open reports on the project track, in projects this user
    #: collaborates on, that they have not read.
    unread_reports: int = 0
    #: The same on the platform track, across every project. Always 0 for a
    #: caller without admin scope -- there is no queue for them to clear.
    unread_platform_reports: int = 0


class NewsletterRequest(Schema):
    email: EmailStr
