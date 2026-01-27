from collections import defaultdict

from fastapi import WebSocket
from pydantic import UUID4

from ainterviewer.interfaces import (
    IOProtocol,
    OutgoingData,
    OutgoingMessage,
    ReceivedData,
)
from ainterviewer.settings import settings as lib_settings
from ainterviewer.types import MessageRole, MessageType

from .embed.main import EmbeddingTask, message_queue
from .settings import app_settings
from .types import WebSocketConversation, WebSocketUsers


class WebsocketMessageHandler(IOProtocol):
    def __init__(self, websocket: WebSocket, project_id: UUID4, interview_id: UUID4):
        self.ws: WebSocket = websocket
        self.project_id = project_id
        self.interview_id = interview_id

    async def send_data(self, data: OutgoingData | OutgoingMessage):
        await self.ws.send_json(data.model_dump())

        if isinstance(data, OutgoingMessage):
            # Add to embedding queue
            embedding_task = EmbeddingTask(
                message_id=data.message_id,
                content=data.content,
                priority=0,
            )
            await message_queue.enqueue(embedding_task)

    async def receive_message(
        self,
        message_id: int,
        message_type: MessageType | None = None,
    ) -> tuple[str, MessageType]:
        data = ReceivedData(**await self.ws.receive_json())

        if data.type in ("message", "audio"):
            text = data.content
            message_type = MessageType.TEXT if not message_type else message_type

            # Add to embedding queue
            embedding_task = EmbeddingTask(
                message_id=message_id,
                content=text,
                priority=1,
            )
            await message_queue.enqueue(embedding_task)

        elif data.type == "image":
            # NOTE: The actual images are send over API, and the path is send
            # over the websocket

            # TODO: Save the image to cloud storage or custom path

            if not data.file:
                raise ValueError()

            image_path = (
                lib_settings.storage.interview_storage.image_path(self.interview_id)
                / data.file
            )

            # FIXME: This should not happen in this class / method
            image_description = self.visual_agent.describe_image(image_path)

            # TODO: Add image description to the database
            # NOTE: The image description is added to the interview_messages

            text = (
                "The user uploaded an image with the following description:\n\n"
                + image_description
            )
        else:
            raise ValueError("Invalid message type")

        return text, message_type or data.type


def create_websocket_interview() -> WebSocketConversation:
    return {"message_count": 0, "users": WebSocketUsers()}


class WebSocketConnectionManager:
    def __init__(self):
        self.active_connections: dict[int, dict[int, WebSocketConversation]] = (
            defaultdict(lambda: defaultdict(create_websocket_interview))  # type: ignore
        )

    async def connect(
        self,
        websocket: WebSocket,
        project_id: int,
        interview_id: int,
        role: MessageRole = MessageRole.USER,
    ):
        await websocket.accept()
        await websocket.send_json({"type": "data", "interview_id": interview_id})
        interview = self.active_connections[project_id][interview_id]
        interview["users"][role.value] = websocket

        if lib_settings.debug:
            print(f"Connected to {project_id} {interview_id} {role}")

    def get_active_interview(
        self, project_id: int, interview_id: int
    ) -> WebSocketConversation:
        if project_id in self.active_connections:
            if interview_id in self.active_connections[project_id]:
                return self.active_connections[project_id][interview_id]
            else:
                raise ValueError(
                    f"No active interviews for interview {interview_id} in interview {project_id}"
                )
        else:
            raise ValueError(f"No active interviews for interview {project_id}")

    def remove_interview(self, project_id: int, interview_id: int):
        del self.active_connections[project_id][interview_id]
        if len(self.active_connections[project_id]) == 0:
            del self.active_connections[project_id]

    async def disconnect(
        self,
        project_id: int,
        interview_id: int,
        role: MessageRole,
    ):
        # NOTE: Should the interview be deleted from this manager
        # when the final user leaves the chat?
        interview = self.get_active_interview(project_id, interview_id)
        del interview["users"][role]
        if len(interview["users"]) == 0:
            self.remove_interview(project_id, interview_id)
        else:
            for connection in interview["users"].values():
                payload = {"type": "system", "content": f"{role} has left the chat."}
                await connection.send_json(payload)

    async def send_personal_message(
        self,
        payload: dict,
        project_id: int,
        interview_id: int,
        role: MessageRole,
    ):
        interview = self.get_active_interview(project_id, interview_id)
        if role not in interview["users"]:
            raise ValueError(
                f"No active connections for {role} in interview {interview_id} in interview {project_id}"
            )
        await interview["users"][role].send_json(payload)

    async def broadcast_message(self, payload: dict):
        connections = [
            connection
            for interview in self.active_connections.values()
            for connection, role in interview.items()
            if role == "user"
        ]
        for connection in connections:
            await connection.send_json(payload)
