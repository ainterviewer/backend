# from collections import defaultdict
#
# from fastapi import WebSocket
#
# from ainterviewer.settings import settings as lib_settings
# from ainterviewer.types import MessageRole
#
# from ...types import WebSocketConversation, WebSocketUsers
#
#
# def create_websocket_interview() -> WebSocketConversation:
#     return {"message_count": 0, "users": WebSocketUsers()}
#
#
# class WebSocketConnectionManager:
#     def __init__(self):
#         self.active_connections: dict[int, dict[int, WebSocketConversation]] = (
#             defaultdict(lambda: defaultdict(create_websocket_interview))
#         )
#
#     async def connect(
#         self,
#         websocket: WebSocket,
#         project_id: int,
#         interview_id: int,
#         role: MessageRole = MessageRole.USER,
#     ):
#         await websocket.accept()
#         await websocket.send_json({"type": "data", "interview_id": interview_id})
#         interview = self.active_connections[project_id][interview_id]
#         interview["users"][role.value] = websocket
#
#         if lib_settings.debug:
#             print(f"Connected to {project_id} {interview_id} {role}")
#
#     def get_active_interview(
#         self, project_id: int, interview_id: int
#     ) -> WebSocketConversation:
#         if project_id in self.active_connections:
#             if interview_id in self.active_connections[project_id]:
#                 return self.active_connections[project_id][interview_id]
#             else:
#                 raise ValueError(
#                     f"No active interviews for interview {interview_id} in interview {project_id}"
#                 )
#         else:
#             raise ValueError(f"No active interviews for interview {project_id}")
#
#     def remove_interview(self, project_id: int, interview_id: int):
#         del self.active_connections[project_id][interview_id]
#         if len(self.active_connections[project_id]) == 0:
#             del self.active_connections[project_id]
#
#     async def disconnect(
#         self,
#         project_id: int,
#         interview_id: int,
#         role: MessageRole,
#     ):
#         # NOTE: Should the interview be deleted from this manager
#         # when the final user leaves the chat?
#         interview = self.get_active_interview(project_id, interview_id)
#         del interview["users"][role]
#         if len(interview["users"]) == 0:
#             self.remove_interview(project_id, interview_id)
#         else:
#             for connection in interview["users"].values():
#                 payload = {"type": "system", "content": f"{role} has left the chat."}
#                 await connection.send_json(payload)
#
#     async def send_personal_message(
#         self,
#         payload: dict,
#         project_id: int,
#         interview_id: int,
#         role: MessageRole,
#     ):
#         interview = self.get_active_interview(project_id, interview_id)
#         if role not in interview["users"]:
#             raise ValueError(
#                 f"No active connections for {role} in interview {interview_id} in interview {project_id}"
#             )
#         await interview["users"][role].send_json(payload)
#
#     async def broadcast_message(self, payload: dict):
#         connections = [
#             connection
#             for interview in self.active_connections.values()
#             for connection, role in interview.items()
#             if role == "user"
#         ]
#         for connection in connections:
#             await connection.send_json(payload)
