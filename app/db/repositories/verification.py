import datetime

from pydantic import UUID4
from sqlalchemy import delete, select, update

from ainterviewer.settings import settings as lib_settings
from ainterviewer.utils import now

from ..tables import VerificationCodeTable
from ..types import VerificationPurpose
from .base import BaseRepository


def _as_aware(value: datetime.datetime) -> datetime.datetime:
    """SQLite returns naive datetimes; attach the library tz for comparison."""
    if value.tzinfo is None:
        return value.replace(tzinfo=lib_settings.tzinfo)
    return value


class VerificationRepository(BaseRepository):
    """Repository for one-time verification codes (email verification + login OTP)."""

    def create(
        self,
        user_id: UUID4,
        code_hash: str,
        purpose: VerificationPurpose,
        expires_at: datetime.datetime,
    ) -> VerificationCodeTable:
        code = VerificationCodeTable(
            user_id=user_id,
            code_hash=code_hash,
            purpose=purpose,
            expires_at=expires_at,
        )
        self.session.add(code)
        self.session.commit()
        self.session.refresh(code)
        return code

    def get_active_by_hash(
        self, code_hash: str, purpose: VerificationPurpose
    ) -> VerificationCodeTable | None:
        """Look up an unconsumed, unexpired code by its hash (magic-link flow)."""
        code = (
            self.session.query(VerificationCodeTable)
            .filter(
                VerificationCodeTable.code_hash == code_hash,
                VerificationCodeTable.purpose == purpose,
                VerificationCodeTable.consumed_at.is_(None),
            )
            .first()
        )
        if code is None or _as_aware(code.expires_at) < now():
            return None
        return code

    def get_active_for_user(
        self, user_id: UUID4, purpose: VerificationPurpose
    ) -> VerificationCodeTable | None:
        """Most recent unconsumed, unexpired code for a user (OTP flow), so we
        can track attempts against the row even when the submitted code is wrong."""
        code = (
            self.session.query(VerificationCodeTable)
            .filter(
                VerificationCodeTable.user_id == user_id,
                VerificationCodeTable.purpose == purpose,
                VerificationCodeTable.consumed_at.is_(None),
            )
            .order_by(VerificationCodeTable.created_at.desc())
            .first()
        )
        if code is None or _as_aware(code.expires_at) < now():
            return None
        return code

    def in_cooldown(
        self, user_id: UUID4, purpose: VerificationPurpose, cooldown_seconds: float
    ) -> bool:
        """True if a code for this purpose was issued within the cooldown window.
        Considers all codes (consumed or not) so rapid re-requests are throttled."""
        last_created = self.session.execute(
            select(VerificationCodeTable.created_at)
            .where(
                VerificationCodeTable.user_id == user_id,
                VerificationCodeTable.purpose == purpose,
            )
            .order_by(VerificationCodeTable.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if last_created is None:
            return False
        return (now() - _as_aware(last_created)).total_seconds() < cooldown_seconds

    def consume(self, code_id: UUID4) -> None:
        self.session.execute(
            update(VerificationCodeTable)
            .where(VerificationCodeTable.id == code_id)
            .values(consumed_at=now())
        )
        self.session.commit()

    def increment_attempts(self, code_id: UUID4) -> int:
        """Increment and return the attempt counter for a code."""
        self.session.execute(
            update(VerificationCodeTable)
            .where(VerificationCodeTable.id == code_id)
            .values(attempts=VerificationCodeTable.attempts + 1)
        )
        self.session.commit()
        return self.session.execute(
            select(VerificationCodeTable.attempts).where(
                VerificationCodeTable.id == code_id
            )
        ).scalar_one()

    def invalidate_active(self, user_id: UUID4, purpose: VerificationPurpose) -> None:
        """Consume any outstanding codes so only one is ever active per purpose."""
        self.session.execute(
            update(VerificationCodeTable)
            .where(
                VerificationCodeTable.user_id == user_id,
                VerificationCodeTable.purpose == purpose,
                VerificationCodeTable.consumed_at.is_(None),
            )
            .values(consumed_at=now())
        )
        self.session.commit()

    def delete(self, code_id: UUID4) -> None:
        self.session.execute(
            delete(VerificationCodeTable).where(VerificationCodeTable.id == code_id)
        )
        self.session.commit()

    def cleanup_expired(self) -> int:
        result = self.session.execute(
            delete(VerificationCodeTable).where(VerificationCodeTable.expires_at < now())
        )
        self.session.commit()
        return result.rowcount  # ty:ignore[unresolved-attribute]
