import sqlalchemy.exc
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from jose import JWTError

from ainterviewer.utils import now

from ..auth import create_auth_token, decode_auth_token, verify_password
from ..db.models import AccessRequestCreate, UserCreate, UserPublic
from ..dependencies import DBSession, DemoToken, auth_cookie_scheme
from ..services.email.mail import send_email
from ..settings import app_settings
from ..types import Scope
from .request_models import LoginData

router = APIRouter(tags=["auth"])


@router.post("/login")
async def login(login_request: LoginData, db: DBSession):
    exception = HTTPException(status_code=403, detail="Invalid email or password")
    try:
        user = db.users.get_user_private(login_request.email)
    except sqlalchemy.exc.NoResultFound:
        raise exception

    if not verify_password(login_request.password, user.password):
        raise exception

    db.users.update_user_status(user.id, last_login=True)

    token = create_auth_token(
        user_id=user.id,
        scope=user.scope,
    )

    response = JSONResponse({"detail": "Successfully logged in"})

    # TODO:
    # - rename token -> access_token
    # - Add refresh token
    #   - login_request.extended sets an expire time for the refresh cookie

    response.set_cookie(
        key="token",
        value=token,
        secure=True,
        httponly=True,
    )

    return response


@router.get("/me")
async def me(db: DBSession, jwt: DemoToken) -> UserPublic:
    return UserPublic.model_validate(db.users.get_user_private(user_id=jwt.user_id))


@router.post("/register")
async def register(
    user: UserCreate,
    db: DBSession,
) -> JSONResponse:
    # NOTE: Overwrite user scope to user when created through the API.
    # - should this be done at the model level?
    user.scope = Scope.USER

    if app_settings.app.registration_requires_token is True:
        if not user.invite_token:
            return JSONResponse({"detail": "Invite token required"}, status_code=406)
        else:
            try:
                invitation = db.users.check_invite_token(user.invite_token.id)
                if not invitation.reuseable:
                    db.users.delete_invitation(invitation.id)

            except sqlalchemy.exc.NoResultFound:
                return JSONResponse({"detail": "Invalid invite token"}, status_code=406)
            if invitation.expires_at < now():
                return JSONResponse({"detail": "Invite token expired"}, status_code=406)

    # NOTE: Password hashing happens in the users.create_user method
    new_user = db.users.create_user(UserCreate(**user.model_dump()))

    token = create_auth_token(user_id=new_user.id)

    response = JSONResponse({"detail": "Successfully registered user"})

    response.set_cookie(
        key="token",
        value=token,
        secure=True,
        httponly=True,
    )

    return response


@router.post("/request-access")
async def request_access(access_request: AccessRequestCreate, db: DBSession):
    try:
        db.users.create_access_request(access_request)
        await send_email(
            app_settings.services.email.recipient.email,
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
async def logout():
    # TODO:
    # - Should this be different for guests vs users?
    # - Delete the token from the database?
    # - For guests, this should be recorded in the database.
    response = JSONResponse({"detail": "Successfully logged out"})
    response.delete_cookie(
        key="token",
        secure=True,
        httponly=True,
        samesite="none",
    )
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
            samesite="none",
        )
    return response


@router.post("/refresh")
async def refresh_token(
    db: DBSession,
    current_token: str = Depends(auth_cookie_scheme),
):
    """Validate the current token and issue a new one if valid."""
    if not current_token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        # Decode and validate the token
        payload = decode_auth_token(current_token)

        user = db.users.get_user_by_id(payload.user_id)

        # Create a new token
        new_token = create_auth_token(
            user_id=user.id,
            scope=user.scope,
        )

        response = JSONResponse({"detail": "Token refreshed successfully"})
        response.set_cookie(
            key="token",
            value=new_token,
            secure=True,
            httponly=True,
        )
        return response

    except (JWTError, sqlalchemy.exc.NoResultFound):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
