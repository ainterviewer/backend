from uuid import UUID

from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter
from sqlalchemy import select

from ..tables import AssistanceMessageChunkTable, AssistanceSessionTable
from .base import BaseRepository


class AssistanceRepository(BaseRepository):
    """Chat sessions with the guide assistant, each owned by one user in one project.

    The session id reaches the server in a cookie the client can rewrite, so
    every read goes through the owner: a session id alone opens nothing.
    """

    def create_new_session(self, project_id: UUID, user_id: UUID) -> UUID:
        session = AssistanceSessionTable(project_id=project_id, user_id=user_id)
        self.session.add(session)
        self.session.commit()
        return session.id

    def owned_session(
        self, session_id: UUID, project_id: UUID, user_id: UUID
    ) -> UUID | None:
        """`session_id` if it is this user's session in this project, else None."""
        return self.session.scalar(
            select(AssistanceSessionTable.id).where(
                AssistanceSessionTable.id == session_id,
                AssistanceSessionTable.project_id == project_id,
                AssistanceSessionTable.user_id == user_id,
            )
        )

    def get_messages(
        self, session_id: UUID, project_id: UUID, user_id: UUID
    ) -> list[ModelMessage]:
        chunks = self.session.scalars(
            select(AssistanceMessageChunkTable.messages_json)
            .join(AssistanceSessionTable)
            .where(
                AssistanceMessageChunkTable.session_id == session_id,
                AssistanceSessionTable.project_id == project_id,
                AssistanceSessionTable.user_id == user_id,
            )
            .order_by(AssistanceMessageChunkTable.created_at)
        )
        return [
            m for chunk in chunks for m in ModelMessagesTypeAdapter.validate_json(chunk)
        ]

    def add_messages(self, messages_json: bytes | str, session_id: UUID) -> None:
        """Append one run's messages to a session the caller has already resolved
        through `owned_session` or `create_new_session`."""
        if isinstance(messages_json, bytes):
            messages_json = messages_json.decode()
        self.session.add(
            AssistanceMessageChunkTable(
                session_id=session_id, messages_json=messages_json
            )
        )
        self.session.commit()
