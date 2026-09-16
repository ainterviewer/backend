from sqlalchemy import Select, delete
from sqlalchemy.orm import Session

from ..tables import (
    CodingTable,
    EmbeddingTable,
    MessageCommentTable,
)


class BaseRepository:
    """Base class for all repositories providing shared session access."""

    def __init__(self, session: Session):
        self.session: Session = session

    def _delete_message_children(self, message_ids: Select) -> None:
        """Delete the analysis rows hanging off a set of messages.

        Call this before deleting messages with a Core `delete()`. Codings and
        comments both declare ON DELETE CASCADE, but SQLite only enforces
        foreign keys on connections that ran
        `PRAGMA foreign_keys=ON` and the app does not currently do that, so the
        cascade never fires and the rows are orphaned instead -- which is how
        the orphaned task and interviewee rows already in the database got
        there. An ORM `session.delete()` on the message does not need this: it
        cascades through the relationships in Python.

        `message_ids` is a SELECT of the message ids, not a materialized list,
        so it must still resolve when this runs -- i.e. before the messages
        themselves are deleted.

        The order is child-first and correct whether or not the cascade fires.
        """
        self.session.execute(
            delete(CodingTable).where(CodingTable.message_id.in_(message_ids))
        )
        self.session.execute(
            delete(MessageCommentTable).where(
                MessageCommentTable.message_id.in_(message_ids)
            )
        )
        # Only the per-message vectors: a QA-pair or interview vector carries a
        # NULL message_id and belongs to the interview, which is deleted (and
        # cleaned up) separately.
        self.session.execute(
            delete(EmbeddingTable).where(EmbeddingTable.message_id.in_(message_ids))
        )
