from uuid import UUID

from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

from ..tables import AssistanceMessageChunkTable, AssistanceSessionTable
from .base import BaseRepository


class AssistanceRepository(BaseRepository):
    def create_new_session(self, project_id: UUID, user_id: UUID) -> UUID:
        session = AssistanceSessionTable(project_id=project_id, user_id=user_id)
        self.session.add(session)
        self.session.commit()
        return session.id

    def get_messages(
        self, session_id: UUID, project_id: UUID, user_id: UUID
    ) -> list[ModelMessage]:
        chunks = (
            self.session.query(AssistanceMessageChunkTable)
            .filter(AssistanceMessageChunkTable.session_id == session_id)
            .order_by(AssistanceMessageChunkTable.created_at)
            .all()
        )
        messages: list[ModelMessage] = []
        for chunk in chunks:
            messages.extend(ModelMessagesTypeAdapter.validate_json(chunk.messages_json))
        return messages

    def add_messages(
        self,
        messages_json: bytes | str,
        session_id: UUID,
        project_id: UUID,
        user_id: UUID,
    ) -> None:
        if isinstance(messages_json, bytes):
            messages_json = messages_json.decode()
        chunk = AssistanceMessageChunkTable(
            session_id=session_id,
            messages_json=messages_json,
        )
        self.session.add(chunk)
        self.session.commit()
