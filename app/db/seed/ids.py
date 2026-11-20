"""Stable resource IDs, scoped to a single development seed session."""

from collections import Counter
from contextlib import contextmanager
from hashlib import sha256
from uuid import UUID

from sqlalchemy import event
from sqlalchemy.orm import Session


def seed_uuid(table: str, number: int) -> UUID:
    """Return a reproducible UUID accepted by the API's UUID4 schemas.

    Each table has its own sequence, so adding messages does not shift project
    IDs. These IDs are development fixtures, never credentials or tokens.
    """
    digest = sha256(f"ainterviewer:seed-dev:{table}:{number}".encode()).digest()
    return UUID(bytes=digest[:16], version=4)


@contextmanager
def stable_ids(session: Session):
    """Supply missing row IDs before SQLAlchemy runs its random defaults."""
    counts: Counter[str] = Counter()

    def assign_ids(session: Session, flush_context, instances) -> None:
        for row in session.new:
            if row.id is None:
                table = row.__table__.name
                counts[table] += 1
                row.id = seed_uuid(table, counts[table])

    event.listen(session, "before_flush", assign_ids)
    try:
        yield
    finally:
        event.remove(session, "before_flush", assign_ids)
