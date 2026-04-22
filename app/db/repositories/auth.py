import datetime

from pydantic import UUID4
from sqlalchemy import delete, update

from ainterviewer.utils import now

from ..tables import RefreshTokenTable
from .base import BaseRepository


class AuthRepository(BaseRepository):
    """Repository for refresh token CRUD operations."""

    def create_refresh_token(
        self,
        user_id: UUID4,
        token_hash: str,
        family_id: UUID4,
        expires_at: datetime.datetime,
        extended: bool = False,
    ) -> RefreshTokenTable:
        token = RefreshTokenTable(
            user_id=user_id,
            token_hash=token_hash,
            family_id=family_id,
            expires_at=expires_at,
            extended=extended,
        )
        self.session.add(token)
        self.session.commit()
        return token

    def get_by_token_hash(self, token_hash: str) -> RefreshTokenTable | None:
        return (
            self.session.query(RefreshTokenTable)
            .filter(RefreshTokenTable.token_hash == token_hash)
            .first()
        )

    def mark_as_used(self, token_id: UUID4) -> None:
        self.session.execute(
            update(RefreshTokenTable)
            .where(RefreshTokenTable.id == token_id)
            .values(is_used=True)
        )
        self.session.commit()

    def revoke_family(self, family_id: UUID4) -> None:
        self.session.execute(
            update(RefreshTokenTable)
            .where(RefreshTokenTable.family_id == family_id)
            .values(is_revoked=True)
        )
        self.session.commit()

    def revoke_all_for_user(self, user_id: UUID4) -> None:
        self.session.execute(
            update(RefreshTokenTable)
            .where(RefreshTokenTable.user_id == user_id)
            .values(is_revoked=True)
        )
        self.session.commit()

    def revoke_token(self, token_id: UUID4) -> None:
        self.session.execute(
            update(RefreshTokenTable)
            .where(RefreshTokenTable.id == token_id)
            .values(is_revoked=True)
        )
        self.session.commit()

    def cleanup_expired(self) -> int:
        result = self.session.execute(
            delete(RefreshTokenTable).where(RefreshTokenTable.expires_at < now())
        )
        self.session.commit()
        return result.rowcount  # ty:ignore[unresolved-attribute]
