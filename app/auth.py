import hashlib
import logging
import secrets
from typing import Any, Self
from uuid import UUID, uuid4

from jose import jwt
from passlib.context import CryptContext
from pydantic import UUID4, BaseModel, PrivateAttr

from ainterviewer.types import Interviewer, InterviewRole, TimeDelta
from ainterviewer.utils import now

from .settings import app_settings
from .types import Scope

# FIXME:
# https://fastapi.tiangolo.com/tutorial/security/oauth2-jwt/?h=oauth#hash-and-verify-the-passwords
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# NOTE: Supress warning raised from bcrypt
logging.getLogger("passlib").setLevel(logging.ERROR)


class _Token(BaseModel):
    _timedelta: TimeDelta = PrivateAttr()

    def encode(self) -> str:
        payload = dict(self)

        payload["exp"] = now() + self._timedelta.to_timedelta()

        serializable_payload = {
            k: str(v) if isinstance(v, UUID) else v for k, v in payload.items()
        }

        return jwt.encode(
            serializable_payload,
            app_settings.secrets.jwt_secret_key.get_secret_value(),
            algorithm="HS256",
        )

    @classmethod
    def decode(cls, token: str) -> Self:
        payload = jwt.decode(
            token,
            app_settings.secrets.jwt_secret_key.get_secret_value(),
            algorithms=["HS256"],
        )

        return cls(**payload)


# FIXME: Make sure this is validated in new setup
class InterviewToken(_Token):
    project_id: UUID4
    interview_id: UUID4
    interviewer: Interviewer
    role: InterviewRole = InterviewRole.RESPONDENT

    _timedelta: TimeDelta = app_settings.app.jwt_interview_token_expiration


class AuthToken(_Token):
    user_id: UUID4
    scope: Scope

    _timedelta: TimeDelta = app_settings.app.jwt_auth_token_expiration


class AssistanceSessionToken(BaseModel):
    project_id: UUID4
    session_id: UUID4


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def generate_refresh_token() -> str:
    """Generate a cryptographically secure random refresh token."""
    return secrets.token_urlsafe(64)


def hash_token(raw_token: str) -> str:
    """SHA-256 hash a raw refresh token for DB storage."""
    return hashlib.sha256(raw_token.encode()).hexdigest()


def create_interview_token(
    project_id: UUID4,
    interview_id: UUID4,
    interviewer: Interviewer = Interviewer.AI,
) -> str:
    token = InterviewToken(
        project_id=project_id,
        interview_id=interview_id,
        interviewer=interviewer,
    )

    return token.encode()


def create_auth_token(
    user_id: UUID4,
    scope: Scope,
) -> str:
    token = AuthToken(
        user_id=user_id,
        scope=scope,
    )

    return token.encode()


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
    parser.add_argument("-c", "--interview_id", type=str, default=None)
    parser.add_argument("-i", "--project_id", type=str, default=None)

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if project_id := args.project_id is None:
        project_id = uuid4()
    if interview_id := args.interview_id is None:
        interview_id = uuid4()

    token = create_interview_token(
        project_id=project_id,
        interview_id=interview_id,
    )
