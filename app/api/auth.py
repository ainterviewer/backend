import logging
import secrets
from datetime import datetime
from uuid import uuid4

import sqlalchemy.exc
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.security import APIKeyCookie

from ainterviewer.settings import settings as lib_settings
from ainterviewer.utils import now

from ..auth import (
    create_auth_token,
    generate_login_code,
    generate_refresh_token,
    generate_verification_token,
    get_password_hash,
    hash_token,
    verify_password,
)
from ..db.repositories.errors import EmailTakenError
from ..dependencies import DBSession, DemoToken
from ..errors import AppError
from ..schemas.common import ActionResponse
from ..schemas.users import (
    AccessRequestCreate,
    AuthResponse,
    DeleteAccountRequest,
    ForgotPasswordRequest,
    InvitationPublic,
    LoginRequest,
    RegisterRequest,
    ResendVerificationRequest,
    ResetPasswordRequest,
    UpdateEmailRequest,
    UpdatePasswordRequest,
    UserCreate,
    UserPublic,
    UserRecord,
    UserSelfUpdate,
    VerifyEmailRequest,
    VerifyLoginCodeRequest,
)
from ..services.email.mail import email_templates, send_email
from ..settings import app_settings
from ..types import ErrorCode, VerificationPurpose
from ..utils import as_aware
from .websockets.manager import session_manager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

refresh_cookie_scheme = APIKeyCookie(name="refresh_token", auto_error=False)

# Pre-computed dummy hash for constant-time login failure (prevents timing attacks)
_DUMMY_HASH = get_password_hash("timing-attack-dummy-password")


def _set_auth_cookies(
    response: Response,
    access_token: str,
    refresh_token: str,
    extended: bool = False,
) -> None:
    """Set the session's cookies.

    The refresh cookie of an `extended` ("remember me") session outlives the
    browser, for as long as the token itself. Any other session's cookies
    are session cookies, gone when the browser closes. The access cookie is
    always one: after a restart the refresh cookie alone gets a new one.
    """
    response.set_cookie(
        key="access_token",
        value=access_token,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        max_age=int(
            app_settings.app.jwt_refresh_token_extended_expiration.to_timedelta().total_seconds()
        )
        if extended
        else None,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )


def _delete_auth_cookies(response: Response) -> None:
    response.delete_cookie(
        key="access_token",
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )
    response.delete_cookie(
        key="refresh_token",
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )


def _create_and_store_refresh_token(
    db,
    user_id,
    extended: bool = False,
    family_id=None,
) -> str:
    """Generate a refresh token, store its hash in the DB, and return the raw token."""
    if family_id is None:
        family_id = uuid4()

    if extended:
        expiry_delta = app_settings.app.jwt_refresh_token_extended_expiration
    else:
        expiry_delta = app_settings.app.jwt_refresh_token_expiration

    raw_refresh = generate_refresh_token()
    db.auth.create_refresh_token(
        user_id=user_id,
        token_hash=hash_token(raw_refresh),
        family_id=family_id,
        expires_at=now() + expiry_delta.to_timedelta(),
        extended=extended,
    )
    return raw_refresh


class AccountExpiredError(AppError):
    """The account's `expires_at` has passed. Raised once the caller has
    proven who they are, so it tells nobody else whether an address has an
    account."""

    status = 403
    code = ErrorCode.ACCOUNT_EXPIRED

    def __init__(self, status: int = 403):
        super().__init__("This account has expired.", status=status)


def _is_expired(expires_at: datetime | None) -> bool:
    return expires_at is not None and as_aware(expires_at) <= now()


def _issue_session(
    db: DBSession, response: Response, user: UserRecord, extended: bool
) -> AuthResponse:
    """Mark the user logged in and set fresh auth cookies on `response`."""
    if _is_expired(user.expires_at):
        raise AccountExpiredError
    db.users.update_user_status(user.id, last_login=True)

    access_token = create_auth_token(
        user_id=user.id, scope=user.scope, preview_features=user.with_preview_features
    )
    raw_refresh = _create_and_store_refresh_token(
        db, user_id=user.id, extended=extended
    )

    # Opportunistically clean up expired refresh tokens
    db.auth.cleanup_expired()

    _set_auth_cookies(response, access_token, raw_refresh, extended)
    return AuthResponse(message="Successfully logged in")


class EmailSendError(AppError):
    """An email the request depends on could not be sent. 502, so the user
    knows to try again rather than to fix what they typed."""

    status = 502
    code = ErrorCode.EMAIL_SEND_FAILED

    def __init__(self):
        super().__init__("Failed to send email. Please try again.")


async def _send_verification_email(db, user: UserPublic | UserRecord) -> None:
    """Issue a fresh email-verification magic link and email it to the user.

    On send failure the just-created code row is deleted and a 502 is raised so
    no orphan state lingers and the resend cooldown isn't poisoned."""
    raw_token = generate_verification_token()
    db.verification.invalidate_active(user.id, VerificationPurpose.EMAIL_VERIFICATION)
    code = db.verification.create(
        user_id=user.id,
        code_hash=hash_token(raw_token),
        purpose=VerificationPurpose.EMAIL_VERIFICATION,
        expires_at=now()
        + app_settings.app.email_verification_token_expiration.to_timedelta(),
    )
    link = (
        f"{app_settings.sveltekit_platform_public_addr}/verify-email?token={raw_token}"
    )
    try:
        await send_email(
            user.email,
            "Verify your email address",
            html_content=email_templates.get_template("verify_email.jinja").render(
                recipient_name=user.first_name,
                verification_link=link,
            ),
        )
    except Exception:
        logger.exception("Failed to send verification email to %s", user.email)
        db.verification.delete(code.id)
        raise EmailSendError from None


async def _send_login_code(db, user: UserRecord) -> None:
    """Issue a fresh one-time login code and email it to the user.

    On send failure the code row is deleted and a 502 is raised so the user can
    immediately retry without tripping the resend cooldown."""
    raw_code = generate_login_code()
    db.verification.invalidate_active(user.id, VerificationPurpose.LOGIN)
    code = db.verification.create(
        user_id=user.id,
        code_hash=hash_token(raw_code),
        purpose=VerificationPurpose.LOGIN,
        expires_at=now() + app_settings.app.login_code_expiration.to_timedelta(),
    )
    try:
        await send_email(
            user.email,
            "Your sign-in code",
            html_content=email_templates.get_template("signin_code.jinja").render(
                recipient_name=user.first_name,
                verification_code=raw_code,
            ),
        )
    except Exception:
        logger.exception("Failed to send login code to %s", user.email)
        db.verification.delete(code.id)
        raise EmailSendError from None


@router.post("/login", responses={202: {"model": AuthResponse}})
async def login(
    login_request: LoginRequest, db: DBSession, response: Response
) -> AuthResponse:
    """Start a session, or with two-factor on, email a code and answer 202
    with `two_factor_required`; `/login/verify-code` then starts it."""
    exception = HTTPException(status_code=403, detail="Invalid email or password")
    try:
        user = db.users.get_user_private(login_request.email)
    except sqlalchemy.exc.NoResultFound:
        verify_password(login_request.password, _DUMMY_HASH)
        raise exception

    if not verify_password(login_request.password, user.password):
        raise exception

    if not user.email_verified:
        raise AppError(
            "Email not verified. Please check your inbox for the verification link.",
            status=403,
            code=ErrorCode.EMAIL_NOT_VERIFIED,
        )

    # Before a code is emailed, not only when a session is issued: a code
    # that can never be redeemed is not worth sending.
    if _is_expired(user.expires_at):
        raise AccountExpiredError

    if user.two_factor_enabled:
        if db.verification.in_cooldown(
            user.id,
            VerificationPurpose.LOGIN,
            app_settings.app.code_resend_cooldown_seconds,
        ):
            raise HTTPException(
                status_code=429,
                detail="A code was just sent. Please wait before requesting another.",
            )
        await _send_login_code(db, user)
        response.status_code = 202
        return AuthResponse(message="Verification code sent", two_factor_required=True)

    return _issue_session(db, response, user, login_request.extended)


@router.post("/login/verify-code")
async def verify_login_code(
    body: VerifyLoginCodeRequest, db: DBSession, response: Response
) -> AuthResponse:
    """Second factor: validate the emailed one-time code and start a session."""
    exception = HTTPException(status_code=403, detail="Invalid or expired code")
    try:
        user = db.users.get_user_private(body.email)
    except sqlalchemy.exc.NoResultFound:
        raise exception

    code = db.verification.get_active_for_user(user.id, VerificationPurpose.LOGIN)
    if code is None:
        raise exception

    attempts = db.verification.increment_attempts(code.id)
    if attempts > app_settings.app.login_code_max_attempts:
        db.verification.consume(code.id)
        raise HTTPException(
            status_code=429,
            detail="Too many attempts. Please request a new code.",
        )

    if not secrets.compare_digest(code.code_hash, hash_token(body.code)):
        raise exception

    db.verification.consume(code.id)
    return _issue_session(db, response, user, body.extended)


@router.get("/me")
async def me(db: DBSession, jwt: DemoToken) -> UserPublic:
    current_user = db.users.get_user_private(user_id=jwt.user_id)

    return UserPublic.model_validate(current_user)


def _verify_current_password(db: DBSession, user_id, password: str) -> UserRecord:
    """Re-authenticate the user for sensitive account operations."""
    user = db.users.get_user_private(user_id=user_id)
    if not verify_password(password, user.password):
        raise HTTPException(status_code=403, detail="Invalid password")
    return user


@router.patch("/me")
async def update_me(body: UserSelfUpdate, db: DBSession, jwt: DemoToken) -> UserPublic:
    return db.users.update_user(jwt.user_id, body)


@router.patch("/me/email")
async def update_my_email(
    body: UpdateEmailRequest, db: DBSession, jwt: DemoToken
) -> UserPublic:
    _verify_current_password(db, jwt.user_id, body.password)

    try:
        return db.users.update_user_email(jwt.user_id, body.new_email)
    except sqlalchemy.exc.IntegrityError:
        raise EmailTakenError from None


@router.post("/me/password")
async def update_my_password(
    body: UpdatePasswordRequest, db: DBSession, jwt: DemoToken, response: Response
) -> ActionResponse:
    user = _verify_current_password(db, jwt.user_id, body.current_password)

    db.users.update_user_password(jwt.user_id, body.new_password)

    # A password change invalidates every existing session, then starts a
    # fresh one so the current client stays logged in.
    db.auth.revoke_all_for_user(jwt.user_id)
    access_token = create_auth_token(
        user_id=user.id, scope=user.scope, preview_features=user.with_preview_features
    )
    raw_refresh = _create_and_store_refresh_token(db, user_id=user.id)

    _set_auth_cookies(response, access_token, raw_refresh)
    return ActionResponse(message="Password updated")


@router.delete("/me")
async def delete_me(
    body: DeleteAccountRequest, db: DBSession, jwt: DemoToken, response: Response
) -> ActionResponse:
    _verify_current_password(db, jwt.user_id, body.password)

    await session_manager.end(db.projects.get_owned_project_ids(jwt.user_id))
    db.users.delete_user(jwt.user_id)

    _delete_auth_cookies(response)
    return ActionResponse(message="Account deleted")


@router.post("/register", status_code=201)
async def register(user: RegisterRequest, db: DBSession) -> ActionResponse:
    snapshot: dict = {}
    claim: InvitationPublic | None = None

    if app_settings.app.registration_requires_token is True:
        if not user.invite_token:
            raise AppError("Invite token required", code=ErrorCode.INVITE_REQUIRED)
        try:
            invitation = db.users.check_invite_token(user.invite_token)
        except sqlalchemy.exc.NoResultFound:
            raise AppError(
                "Invalid invite token", code=ErrorCode.INVITE_INVALID
            ) from None
        if invitation.expires_at and invitation.expires_at < now():
            raise AppError("Invite token expired", code=ErrorCode.INVITE_EXPIRED)
        if not invitation.reuseable:
            claim = invitation
        snapshot["scope"] = invitation.user_scope

        # A token that is not a UUID was the invitation's alias. Keep the id in
        # invite_token either way, and the alias it came through alongside.
        if isinstance(user.invite_token, str):
            snapshot["registration_token"] = invitation.alias
        user.invite_token = invitation.id

        # Snapshot invitation data
        snapshot["invitation_title"] = invitation.title
        snapshot["demo_concurrent_limit"] = invitation.user_demo_concurrent_limit
        snapshot["demo_daily_limit"] = invitation.user_demo_daily_limit
        snapshot["demo_lifetime_limit"] = invitation.user_demo_lifetime_limit
        if invitation.user_expires is not None:
            if isinstance(invitation.user_expires, datetime):
                snapshot["expires_at"] = invitation.user_expires
            else:
                snapshot["expires_at"] = now() + invitation.user_expires.to_timedelta()

        # Snapshot access request data
        if invitation.access_request_id:
            access_request = db.users.get_access_request(invitation.access_request_id)
            snapshot["access_request_message"] = access_request.message
            snapshot["organization"] = access_request.organization

    # NOTE: Password hashing happens in the users.create_user method
    # Raises InvitationClaimedError when another request redeemed a single-use
    # token first, and EmailTakenError for an address with an account.
    new_user = db.users.create_user(
        UserCreate(**user.model_dump(), **snapshot),
        claim_invitation=claim.id if claim else None,
    )

    # Hard gate: no session is issued until the email address is verified.
    # If the email can't be sent, roll back the new account so the address is
    # free for a clean retry rather than being stuck unverified.
    try:
        await _send_verification_email(db, new_user)
    except EmailSendError:
        db.users.delete_user(new_user.id)
        if claim:
            db.users.restore_invitation(claim)
        raise

    # Give every new account a personal folder to start from. Created only
    # after the verification email succeeds, so it can't be orphaned by the
    # rollback above (deleting a user cascades the collaborator row, not the
    # folder itself).
    username = f"{new_user.first_name}'{'s' if new_user.first_name[-1] != 's' else ''}"

    db.projects.create_folder(
        title=f"{username} Personal Folder",
        user_id=new_user.id,
    )

    return ActionResponse(
        message="Registration successful. Please verify your email to continue."
    )


@router.post("/verify-email")
async def verify_email(body: VerifyEmailRequest, db: DBSession) -> ActionResponse:
    """Confirm a sign-up email-verification magic link."""
    code = db.verification.get_active_by_hash(
        hash_token(body.token), VerificationPurpose.EMAIL_VERIFICATION
    )
    if code is None:
        raise AppError(
            "Invalid or expired verification link",
            code=ErrorCode.EMAIL_LINK_INVALID,
        )

    db.users.set_email_verified(code.user_id)
    db.verification.consume(code.id)

    return ActionResponse(message="Email verified successfully")


@router.post("/resend-verification")
async def resend_verification(
    body: ResendVerificationRequest, db: DBSession
) -> ActionResponse:
    """Re-send the verification email. Always returns 200 to avoid leaking
    which addresses are registered."""
    detail = (
        "If the account exists and is unverified, a verification email has been sent."
    )
    try:
        user = db.users.get_user_private(body.email)
    except sqlalchemy.exc.NoResultFound:
        return ActionResponse(message=detail)

    # Skip silently when already verified or still within the resend cooldown,
    # so a 200 can't be used to distinguish those states from a missing account.
    # A genuine SMTP failure still surfaces as a 502 (only ever for a real,
    # unverified account), which is an acceptable, rare-condition leak.
    if not user.email_verified and not db.verification.in_cooldown(
        user.id,
        VerificationPurpose.EMAIL_VERIFICATION,
        app_settings.app.code_resend_cooldown_seconds,
    ):
        await _send_verification_email(db, user)

    return ActionResponse(message=detail)


async def _send_password_reset_email(db, user: UserRecord) -> None:
    """Issue a fresh password-reset magic link and email it to the user.

    On send failure the code row is deleted and a 502 is raised so the user can
    immediately retry without tripping the resend cooldown."""
    raw_token = generate_verification_token()
    db.verification.invalidate_active(user.id, VerificationPurpose.PASSWORD_RESET)
    expiration = app_settings.app.password_reset_token_expiration.to_timedelta()
    code = db.verification.create(
        user_id=user.id,
        code_hash=hash_token(raw_token),
        purpose=VerificationPurpose.PASSWORD_RESET,
        expires_at=now() + expiration,
    )
    link = f"{app_settings.sveltekit_platform_public_addr}/reset-password?token={raw_token}"
    minutes = int(expiration.total_seconds() // 60)
    try:
        await send_email(
            user.email,
            "Reset your password",
            html_content=email_templates.get_template("reset_password.jinja").render(
                recipient_name=user.first_name,
                reset_link=link,
                expires_in=f"{minutes} minute{'s' if minutes != 1 else ''}",
            ),
        )
    except Exception:
        logger.exception("Failed to send password reset email to %s", user.email)
        db.verification.delete(code.id)
        raise EmailSendError from None


@router.post("/forgot-password")
async def forgot_password(body: ForgotPasswordRequest, db: DBSession) -> ActionResponse:
    """Email a password-reset link. Always returns 200 to avoid leaking which
    addresses are registered."""
    detail = (
        "If an account exists with this email, a password reset link has been sent."
    )
    try:
        user = db.users.get_user_private(body.email)
    except sqlalchemy.exc.NoResultFound:
        return ActionResponse(message=detail)

    # Skip silently within the cooldown so a 200 can't distinguish it from a
    # missing account. As with resend-verification, a genuine SMTP failure
    # still surfaces as a 502 for a real account -- a rare-condition leak.
    if not db.verification.in_cooldown(
        user.id,
        VerificationPurpose.PASSWORD_RESET,
        app_settings.app.code_resend_cooldown_seconds,
    ):
        await _send_password_reset_email(db, user)

    return ActionResponse(message=detail)


@router.post("/reset-password")
async def reset_password(body: ResetPasswordRequest, db: DBSession) -> ActionResponse:
    """Set a new password from a password-reset magic link."""
    code = db.verification.get_active_by_hash(
        hash_token(body.token), VerificationPurpose.PASSWORD_RESET
    )
    if code is None:
        raise AppError(
            "Invalid or expired reset link", code=ErrorCode.EMAIL_LINK_INVALID
        )

    db.verification.consume(code.id)
    db.users.update_user_password(code.user_id, body.new_password)
    # Receiving the link proves control of the address, so an account still
    # waiting on its verification email isn't left locked out after a reset.
    db.users.set_email_verified(code.user_id)
    # Whoever knew the old password may hold a session; end all of them. No
    # new session is issued: login still goes through two-factor if enabled.
    db.auth.revoke_all_for_user(code.user_id)

    return ActionResponse(message="Password has been reset")


@router.post("/request-access")
async def request_access(access_request: AccessRequestCreate, db: DBSession):
    try:
        db.users.create_access_request(access_request)
        await send_email(
            app_settings.services.email.recipient.email,  # ty:ignore[unresolved-attribute]
            "New access request",
            body=f"New access request from {access_request.email}\n\nOrganization: {access_request.organization}\nMessage:\n{access_request.message}",
        )
    except sqlalchemy.exc.IntegrityError as e:
        # Check if the error is a UNIQUE constraint violation
        if "UNIQUE constraint failed" in str(e.orig):
            raise AppError(
                "Access request already exists for this email.",
                status=409,
                code=ErrorCode.ACCESS_REQUEST_EXISTS,
            ) from None
        raise


@router.post("/logout")
async def logout(
    db: DBSession,
    response: Response,
    raw_refresh: str | None = Depends(refresh_cookie_scheme),
) -> ActionResponse:
    if raw_refresh:
        stored = db.auth.get_by_token_hash(hash_token(raw_refresh))
        if stored:
            # The whole family, not only this token: its predecessor is still
            # within the reuse grace for a few seconds after every refresh.
            db.auth.revoke_family(stored.family_id)

    _delete_auth_cookies(response)
    return ActionResponse(message="Successfully logged out")


@router.post("/logout-everywhere")
async def logout_everywhere(
    db: DBSession, jwt: DemoToken, response: Response
) -> ActionResponse:
    """Revoke ALL refresh tokens for the authenticated user."""
    db.auth.revoke_all_for_user(jwt.user_id)

    _delete_auth_cookies(response)
    return ActionResponse(message="All sessions revoked")


@router.post("/exit")
async def exit(response: Response) -> ActionResponse:
    """Route to exit the interview and deletes the cookies."""
    cookies_to_delete = {"interview_token", "config"}
    for key in cookies_to_delete:
        response.delete_cookie(
            key=key,
            secure=True,
            httponly=True,
            samesite="lax",
        )
    return ActionResponse(message="Successfully exited")


@router.post("/refresh")
async def refresh(
    db: DBSession,
    response: Response,
    raw_refresh: str | None = Depends(refresh_cookie_scheme),
) -> ActionResponse:
    """Rotate the refresh token and issue a new access token."""
    if not raw_refresh:
        raise HTTPException(status_code=401, detail="No refresh token")

    stored = db.auth.get_by_token_hash(hash_token(raw_refresh))

    if stored is None:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    if stored.is_revoked:
        raise HTTPException(status_code=401, detail="Refresh token revoked")

    if stored.expires_at.replace(tzinfo=lib_settings.tzinfo) < now():
        raise HTTPException(status_code=401, detail="Refresh token expired")

    # Reuse detection: a token spent before may have been stolen, so spending
    # it again ends the whole family -- unless it was spent moments ago. One
    # browser often refreshes several times at once with the same token
    # (parallel server renders, a client call, a second tab); the first wins
    # the rotation and the rest arrive just after. They get a pair of their
    # own in the same family rather than a logout.
    if not db.auth.mark_as_used(stored.id):
        grace = app_settings.app.refresh_token_reuse_grace.to_timedelta()
        if not db.auth.used_since(stored.id, now() - grace):
            db.auth.revoke_family(stored.family_id)
            raise HTTPException(
                status_code=401,
                detail="Refresh token reuse detected, all sessions in this family have been revoked",
            )

    # Look up user for fresh scope (in case it changed since last login)
    user = db.users.get_user_by_id(stored.user_id)

    # An account that expired mid-session ends there: 401, like any other
    # refresh the client cannot recover from, so it is sent to the login page,
    # where signing in again says why.
    if _is_expired(user.expires_at):
        db.auth.revoke_family(stored.family_id)
        raise AccountExpiredError(status=401)

    # No last_active touch here: a backgrounded tab refreshes on its own, so a
    # refresh is not activity. Scope-checked requests record it instead
    # (`_record_activity`, app/dependencies.py).

    # Issue new token pair, same family.
    #
    # TODO(session-lifetime): every rotation starts the new token's lifetime
    # afresh (`now() + expiry`), so a session that keeps refreshing never
    # ends -- there is no absolute cap on a family. See
    # plans/session-lifetime.md before changing how expiry is computed here.
    new_access = create_auth_token(
        user_id=user.id, scope=user.scope, preview_features=user.with_preview_features
    )
    new_raw_refresh = _create_and_store_refresh_token(
        db,
        user_id=user.id,
        extended=stored.extended,
        family_id=stored.family_id,
    )

    _set_auth_cookies(response, new_access, new_raw_refresh, stored.extended)
    return ActionResponse(message="Token refreshed successfully")
