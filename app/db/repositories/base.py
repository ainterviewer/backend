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
        comments both declare ON DELETE CASCADE, which fires while foreign keys
        are enforced (the default) but not where enforcement is switched off
        with `DATABASE__ENFORCE_FOREIGN_KEYS=false` -- and that is how the
        orphans c3d18b40f2a7 cleaned up got there. Deleting them explicitly
        holds either way. An ORM `session.delete()` on the message does not need
        this: it cascades through the relationships in Python.

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
