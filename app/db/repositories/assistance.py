from uuid import UUID

from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter
from sqlalchemy import select

from ..tables import AssistanceMessageChunkTable, AssistanceSessionTable
from .base import BaseRepository


class AssistanceRepository(BaseRepository):
    """Chat sessions with the guide assistant, each owned by one user in one project.

    A user's current session in a project is their newest one: starting a new
    chat creates a session, and the old ones are kept but no longer read.
    Session ids never leave the server, so nothing a client sends picks one.
    """

    def create_new_session(self, project_id: UUID, user_id: UUID) -> UUID:
        session = AssistanceSessionTable(project_id=project_id, user_id=user_id)
        self.session.add(session)
        self.session.commit()
        return session.id

    def current_session(self, project_id: UUID, user_id: UUID) -> UUID | None:
        """This user's newest session in this project, if they have one."""
        return self.session.scalar(
            select(AssistanceSessionTable.id)
            .where(
                AssistanceSessionTable.project_id == project_id,
                AssistanceSessionTable.user_id == user_id,
            )
            .order_by(
                AssistanceSessionTable.created_at.desc(),
                AssistanceSessionTable.id.desc(),
            )
            .limit(1)
        )

    def get_messages(self, session_id: UUID) -> list[ModelMessage]:
        chunks = self.session.scalars(
            select(AssistanceMessageChunkTable.messages_json)
            .where(AssistanceMessageChunkTable.session_id == session_id)
            .order_by(AssistanceMessageChunkTable.created_at)
        )
        return [
            m for chunk in chunks for m in ModelMessagesTypeAdapter.validate_json(chunk)
        ]

    def add_messages(self, messages_json: bytes | str, session_id: UUID) -> None:
        """Append one run's messages to a session."""
        if isinstance(messages_json, bytes):
            messages_json = messages_json.decode()
        self.session.add(
            AssistanceMessageChunkTable(
                session_id=session_id, messages_json=messages_json
            )
        )
        self.session.commit()
