import logging
from datetime import timedelta
from typing import Any
from uuid import UUID

from jose import jwt
from passlib.context import CryptContext
from pydantic import UUID4, BaseModel

from ainterviewer.types import Interviewer, InterviewRole
from ainterviewer.utils import now

from .settings import app_settings
from .types import Scope

# FIXME:
# https://fastapi.tiangolo.com/tutorial/security/oauth2-jwt/?h=oauth#hash-and-verify-the-passwords
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# NOTE: Supress warning raised from bcrypt
logging.getLogger("passlib").setLevel(logging.ERROR)


class InterviewToken(BaseModel):
    project_id: UUID4
    interview_id: UUID4
    interviewer: Interviewer
    role: InterviewRole = InterviewRole.RESPONDENT


class AuthToken(BaseModel):
    user_id: UUID4
    scope: Scope


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def create_token(
    payload: dict[str, Any], timedelta_kwargs: dict[str, float] | None
) -> str:
    if timedelta_kwargs:
        payload["exp"] = now() + timedelta(**timedelta_kwargs)

    serializable_payload = {
        k: str(v) if isinstance(v, UUID) else v for k, v in payload.items()
    }

    return jwt.encode(
        serializable_payload,
        app_settings.secrets.jwt_secret_key.get_secret_value(),
        algorithm="HS256",
    )


def create_interview_token(
    project_id: UUID4,
    interview_id: UUID4,
    interviewer: Interviewer = Interviewer.AI,
) -> str:
    timedelta_kwargs = app_settings.app.jwt_interview_token_expiration

    payload = InterviewToken(
        project_id=project_id,
        interview_id=interview_id,
        interviewer=interviewer,
    )

    return create_token(dict(payload), timedelta_kwargs)


def create_auth_token(
    user_id: UUID4,
    scope: Scope = Scope.USER,
) -> str:
    timedelta_kwargs = app_settings.app.jwt_auth_token_expiration

    payload = AuthToken(
        user_id=user_id,
        scope=scope,
    )

    return create_token(dict(payload), timedelta_kwargs)


def decode_jwt(
    token: str,
) -> dict[str, Any]:
    payload = jwt.decode(
        token,
        app_settings.secrets.jwt_secret_key.get_secret_value(),
        algorithms=["HS256"],
    )
    return payload


def decode_interview_token(token: str) -> InterviewToken:
    return InterviewToken(**decode_jwt(token))


def decode_auth_token(token: str) -> AuthToken:
    return AuthToken(**decode_jwt(token))


def parse_args():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--interview_id", type=int, default=None)
    parser.add_argument("-i", "--project_id", type=int, default=1)

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    token = create_interview_token(
        project_id=args.project_id,
        interview_id=args.interview_id,
    )
