import logging
import secrets
from datetime import datetime
from uuid import uuid4

import sqlalchemy.exc
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
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
from ..db.models import (
    AccessRequestCreate,
    UserCreate,
    UserCreateRequest,
    UserPrivate,
    UserPublic,
    UserSelfUpdate,
)
from ..db.types import VerificationPurpose
from ..dependencies import DBSession, DemoToken
from ..services.email.mail import email_templates, send_email
from ..settings import app_settings
from ..types import Scope
from .request_models import (
    DeleteAccountRequest,
    LoginData,
    ResendVerificationRequest,
    UpdateEmailRequest,
    UpdatePasswordRequest,
    VerifyEmailRequest,
    VerifyLoginCodeRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

refresh_cookie_scheme = APIKeyCookie(name="refresh_token", auto_error=False)

# Pre-computed dummy hash for constant-time login failure (prevents timing attacks)
_DUMMY_HASH = get_password_hash("timing-attack-dummy-password")


def _set_auth_cookies(
    response: JSONResponse,
    access_token: str,
    refresh_token: str,
) -> None:
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
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )


def _delete_auth_cookies(response: JSONResponse) -> None:
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


def _issue_session(
    db,
    user: UserPrivate,
    extended: bool,
    detail: str = "Successfully logged in",
) -> JSONResponse:
    """Mark the user logged in and return a response with fresh auth cookies."""
    db.users.update_user_status(user.id, last_login=True)

    access_token = create_auth_token(user_id=user.id, scope=user.scope)
    raw_refresh = _create_and_store_refresh_token(
        db, user_id=user.id, extended=extended
    )

    # Opportunistically clean up expired refresh tokens
    db.auth.cleanup_expired()

    response = JSONResponse({"detail": detail})
    _set_auth_cookies(response, access_token, raw_refresh)
    return response


_EMAIL_SEND_FAILED = HTTPException(
    status_code=502, detail="Failed to send email. Please try again."
)


async def _send_verification_email(db, user: UserPublic | UserPrivate) -> None:
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
        raise _EMAIL_SEND_FAILED


async def _send_login_code(db, user: UserPrivate) -> None:
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
        raise _EMAIL_SEND_FAILED


@router.post("/login")
async def login(login_request: LoginData, db: DBSession):
    exception = HTTPException(status_code=403, detail="Invalid email or password")
    try:
        user = db.users.get_user_private(login_request.email)
    except sqlalchemy.exc.NoResultFound:
        verify_password(login_request.password, _DUMMY_HASH)
        raise exception

    if not verify_password(login_request.password, user.password):
        raise exception

    if not user.email_verified:
        raise HTTPException(
            status_code=403,
            detail="Email not verified. Please check your inbox for the verification link.",
        )

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
        return JSONResponse(
            {"detail": "Verification code sent", "two_factor_required": True},
            status_code=202,
        )

    return _issue_session(db, user, login_request.extended)


@router.post("/login/verify-code")
async def verify_login_code(body: VerifyLoginCodeRequest, db: DBSession):
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
    return _issue_session(db, user, body.extended)


@router.get("/me")
async def me(db: DBSession, jwt: DemoToken) -> UserPublic:
    current_user = db.users.get_user_private(user_id=jwt.user_id)

    return UserPublic.model_validate(current_user)


def _verify_current_password(db: DBSession, user_id, password: str) -> UserPrivate:
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
        raise HTTPException(status_code=409, detail="Email already in use")


@router.post("/me/password")
async def update_my_password(
    body: UpdatePasswordRequest, db: DBSession, jwt: DemoToken
):
    user = _verify_current_password(db, jwt.user_id, body.current_password)

    db.users.update_user_password(jwt.user_id, body.new_password)

    # A password change invalidates every existing session, then starts a
    # fresh one so the current client stays logged in.
    db.auth.revoke_all_for_user(jwt.user_id)
    access_token = create_auth_token(user_id=user.id, scope=user.scope)
    raw_refresh = _create_and_store_refresh_token(db, user_id=user.id)

    response = JSONResponse({"detail": "Password updated"})
    _set_auth_cookies(response, access_token, raw_refresh)
    return response


@router.delete("/me")
async def delete_me(body: DeleteAccountRequest, db: DBSession, jwt: DemoToken):
    _verify_current_password(db, jwt.user_id, body.password)

    db.users.delete_user(jwt.user_id)

    response = JSONResponse({"detail": "Account deleted"})
    _delete_auth_cookies(response)
    return response


@router.post("/register")
async def register(user: UserCreateRequest, db: DBSession) -> JSONResponse:
    # NOTE: Validate user scope to user when created through the API.
    # - should this be done at the model level?
    if not Scope.USER.includes(user.scope):
        raise ValueError("Invalid scope for user creation")

    snapshot: dict = {}

    if app_settings.app.registration_requires_token is True:
        if not user.invite_token:
            return JSONResponse({"detail": "Invite token required"}, status_code=406)
        elif isinstance(user.invite_token, str):
            if user.invite_token not in app_settings.app.special_registration_tokens:
                return JSONResponse({"detail": "Invalid invite token"}, status_code=406)
            # Special tokens are validated against settings, not stored as
            # invitations. Record which one was used in registration_token and
            # keep the arbitrary string out of the UUID invite_token column.
            snapshot["registration_token"] = user.invite_token
            user.invite_token = None
        else:
            try:
                invitation = db.users.check_invite_token(user.invite_token)
                if not invitation.reuseable:
                    if not db.users.delete_invitation(invitation.id):
                        # Another request already claimed this token
                        return JSONResponse(
                            {"detail": "Invalid invite token"}, status_code=406
                        )
                user.scope = invitation.user_scope
            except sqlalchemy.exc.NoResultFound:
                return JSONResponse({"detail": "Invalid invite token"}, status_code=406)
            if invitation.expires_at and invitation.expires_at < now():
                return JSONResponse({"detail": "Invite token expired"}, status_code=406)

            # Snapshot invitation data
            snapshot["invitation_title"] = invitation.title
            if invitation.user_expires is not None:
                if isinstance(invitation.user_expires, datetime):
                    snapshot["expires_at"] = invitation.user_expires
                else:
                    snapshot["expires_at"] = (
                        now() + invitation.user_expires.to_timedelta()
                    )

            # Snapshot access request data
            if invitation.access_request_id:
                access_request = db.users.get_access_request(
                    invitation.access_request_id
                )
                snapshot["access_request_message"] = access_request.message
                snapshot["organization"] = access_request.organization

    # NOTE: Password hashing happens in the users.create_user method
    new_user = db.users.create_user(UserCreate(**user.model_dump(), **snapshot))

    # Hard gate: no session is issued until the email address is verified.
    # If the email can't be sent, roll back the new account so the address is
    # free for a clean retry rather than being stuck unverified.
    try:
        await _send_verification_email(db, new_user)
    except HTTPException:
        db.users.delete_user(new_user.id)
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

    return JSONResponse(
        {"detail": "Registration successful. Please verify your email to continue."},
        status_code=201,
    )


@router.post("/verify-email")
async def verify_email(body: VerifyEmailRequest, db: DBSession):
    """Confirm a sign-up email-verification magic link."""
    code = db.verification.get_active_by_hash(
        hash_token(body.token), VerificationPurpose.EMAIL_VERIFICATION
    )
    if code is None:
        raise HTTPException(
            status_code=400, detail="Invalid or expired verification link"
        )

    db.users.set_email_verified(code.user_id)
    db.verification.consume(code.id)

    return JSONResponse({"detail": "Email verified successfully"})


@router.post("/resend-verification")
async def resend_verification(body: ResendVerificationRequest, db: DBSession):
    """Re-send the verification email. Always returns 200 to avoid leaking
    which addresses are registered."""
    detail = (
        "If the account exists and is unverified, a verification email has been sent."
    )
    try:
        user = db.users.get_user_private(body.email)
    except sqlalchemy.exc.NoResultFound:
        return JSONResponse({"detail": detail})

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

    return JSONResponse({"detail": detail})


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
            return JSONResponse(
                content={"details": "Access request already exists for this email."},
                status_code=400,
            )
        raise e


@router.post("/logout")
async def logout(
    db: DBSession,
    raw_refresh: str | None = Depends(refresh_cookie_scheme),
):
    if raw_refresh:
        stored = db.auth.get_by_token_hash(hash_token(raw_refresh))
        if stored:
            db.auth.revoke_token(stored.id)

    response = JSONResponse({"detail": "Successfully logged out"})
    _delete_auth_cookies(response)
    return response


@router.post("/logout-everywhere")
async def logout_everywhere(db: DBSession, jwt: DemoToken):
    """Revoke ALL refresh tokens for the authenticated user."""
    db.auth.revoke_all_for_user(jwt.user_id)

    response = JSONResponse({"detail": "All sessions revoked"})
    _delete_auth_cookies(response)
    return response


@router.post("/exit")
async def exit():
    """Route to exit the interview and deletes the cookies."""
    response = JSONResponse({"detail": "Successfully exited"})
    cookies_to_delete = {"language", "interview_token", "config"}
    for key in cookies_to_delete:
        response.delete_cookie(
            key=key,
            secure=True,
            httponly=True,
            samesite="lax",
        )
    return response


@router.post("/refresh")
async def refresh(
    db: DBSession,
    raw_refresh: str | None = Depends(refresh_cookie_scheme),
):
    """Rotate the refresh token and issue a new access token."""
    if not raw_refresh:
        raise HTTPException(status_code=401, detail="No refresh token")

    stored = db.auth.get_by_token_hash(hash_token(raw_refresh))

    if stored is None:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    # Reuse detection: token was already consumed — possible theft
    if stored.is_used:
        db.auth.revoke_family(stored.family_id)
        raise HTTPException(
            status_code=401,
            detail="Refresh token reuse detected, all sessions in this family have been revoked",
        )

    if stored.is_revoked:
        raise HTTPException(status_code=401, detail="Refresh token revoked")

    if stored.expires_at.replace(tzinfo=lib_settings.tzinfo) < now():
        raise HTTPException(status_code=401, detail="Refresh token expired")

    if not db.auth.mark_as_used(stored.id):
        db.auth.revoke_family(stored.family_id)
        raise HTTPException(
            status_code=401,
            detail="Refresh token reuse detected, all sessions in this family have been revoked",
        )

    # Look up user for fresh scope (in case it changed since last login)
    user = db.users.get_user_by_id(stored.user_id)

    # Issue new token pair, same family
    new_access = create_auth_token(user_id=user.id, scope=user.scope)
    new_raw_refresh = _create_and_store_refresh_token(
        db,
        user_id=user.id,
        extended=stored.extended,
        family_id=stored.family_id,
    )

    response = JSONResponse({"detail": "Token refreshed successfully"})
    _set_auth_cookies(response, new_access, new_raw_refresh)
    return response
