import hashlib
import secrets
from datetime import UTC, datetime
from typing import Literal, Self
from uuid import UUID

import bcrypt
from jose import jwt
from pydantic import UUID4, BaseModel, PrivateAttr, field_validator

from ainterviewer.types import Interviewer, InterviewRole, TimeDelta
from ainterviewer.utils import now

from .settings import app_secrets, app_settings
from .types import PreviewFeature, PreviewFeatureList, Scope

#: bcrypt reads only the first 72 bytes of a password. passlib truncated
#: silently, so every stored hash was made from at most this much, and
#: bcrypt >= 5 refuses longer input outright: truncating here keeps long
#: passwords hashing and verifying as they always have.
_BCRYPT_MAX_BYTES = 72


def _password_bytes(password: str) -> bytes:
    return password.encode()[:_BCRYPT_MAX_BYTES]


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
            app_secrets.jwt_secret_key.get_secret_value(),
            algorithm="HS256",
        )

    @classmethod
    def decode(cls, token: str) -> Self:
        payload = jwt.decode(
            token,
            app_secrets.jwt_secret_key.get_secret_value(),
            algorithms=["HS256"],
        )

        return cls(**payload)


class InterviewToken(_Token):
    project_id: UUID4
    interview_id: UUID4
    interviewer: Interviewer
    role: InterviewRole = InterviewRole.RESPONDENT

    _timedelta: TimeDelta = app_settings.app.jwt_interview_token_expiration


class TestShareToken(_Token):
    """A shared test link: lets anyone holding it try one project's draft.

    `kind` keeps it from ever passing for another token signed with the same
    key. Accepted only while `nonce` is the project's `test_share_nonce`, so
    clearing that revokes every link at once.
    """

    kind: Literal["test_share"]
    project_id: UUID4
    nonce: str
    #: Interviews started from the link send their respondent on through the
    #: project's completion redirects, as distributed ones do: for piloting a
    #: panel's return links. A claim so whoever holds the link cannot turn it
    #: on; links minted before it existed decode as off.
    redirect: bool = False

    _timedelta: TimeDelta = app_settings.app.test_share_link_expiration


class AuthToken(_Token):
    user_id: UUID4
    scope: Scope
    # Mirrors UserTable.preview_features, read fresh from the database on every
    # login and refresh, so an admin's change reaches the token within one
    # access-token lifetime. Defaults to none so tokens issued before the claim
    # existed decode as "off" until their next refresh.
    preview_features: PreviewFeatureList = []

    _timedelta: TimeDelta = app_settings.app.jwt_auth_token_expiration

    @field_validator("preview_features", mode="before")
    @classmethod
    def _from_flag(cls, value: object) -> object:
        # The claim was one flag for every feature until the list replaced it.
        # A token signed then reads as all of them or none until its refresh.
        if isinstance(value, bool):
            return list(PreviewFeature) if value else []
        return value

    def has_preview(self, feature: PreviewFeature) -> bool:
        return feature in self.preview_features


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(_password_bytes(plain_password), hashed_password.encode())
    except ValueError:
        # Not a bcrypt hash: no password matches it.
        return False


def get_password_hash(password: str) -> str:
    return bcrypt.hashpw(_password_bytes(password), bcrypt.gensalt()).decode()


def generate_refresh_token() -> str:
    """Generate a cryptographically secure random refresh token."""
    return secrets.token_urlsafe(64)


def generate_verification_token() -> str:
    """Generate a high-entropy token for email-verification magic links."""
    return secrets.token_urlsafe(32)


def generate_resume_token() -> str:
    """Generate a high-entropy token for a one-time interview resume link.

    Same size as the verification token: this is a bearer credential for an
    interview transcript and is guessed at, not derived, so entropy is the
    only thing standing in the way.
    """
    return secrets.token_urlsafe(32)


def generate_login_code() -> str:
    """Generate a 6-digit one-time login code (zero-padded)."""
    return f"{secrets.randbelow(10**6):06d}"


def hash_token(raw_token: str) -> str:
    """SHA-256 hash a raw token/code for DB storage."""
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
    preview_features: list[PreviewFeature],
) -> str:
    token = AuthToken(
        user_id=user_id,
        scope=scope,
        preview_features=preview_features,
    )

    return token.encode()


def create_test_share_token(
    project_id: UUID4, nonce: str, redirect: bool = False
) -> tuple[str, datetime]:
    """A shared test link's token, and when it expires."""
    token = TestShareToken(
        kind="test_share", project_id=project_id, nonce=nonce, redirect=redirect
    ).encode()
    expires_at = datetime.fromtimestamp(jwt.get_unverified_claims(token)["exp"], UTC)
    return token, expires_at
