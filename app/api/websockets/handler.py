from fastapi import WebSocket
from pydantic import UUID4

from ainterviewer.interfaces import (
    IOProtocol,
    OutgoingData,
    OutgoingMessage,
    ReceivedData,
)
from ainterviewer.types import MessageType

from ...embed.main import EmbeddingTask, message_queue


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

            if data.filename is None:
                raise ValueError()

            # image_path = (
            #     lib_settings.storage.interview_storage.image_path(self.interview_id)
            #     / data.filename
            # )

            # FIXME: This should not happen in this class / method
            # image_description = self.visual_agent.describe_image(image_path)

            # TODO: Add image description to the database
            # NOTE: The image description is added to the interview_messages
            #
            # text = (
            #     "The user uploaded an image with the following description:\n\n"
            #     + image_description
            # )

        return text, message_type or data.type  # ty:ignore[invalid-return-type]
