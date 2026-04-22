# from fastapi import (
#     APIRouter,
#     Depends,
#     WebSocket,
#     WebSocketDisconnect,
#     WebSocketException,
# )
# from fastapi.responses import RedirectResponse
# from jose import JWTError
# from pydantic import UUID4
#
# from ainterviewer.types import Interviewer, MessageRole
#
# from ....auth import create_interview_token, decode_jwt
# from ....dependencies import AdminToken, DBSession, get_ws_manager
# from ....utils import replay_history
# from ....websockets import WebSocketConnectionManager
# from ...request_models import BroadcastRequest
#
# router = APIRouter(prefix="/ws", tags=["interviews"])
#
#
# @router.get("/connections")
# async def active_websockets(
#     jwt: AdminToken,
#     manager: WebSocketConnectionManager = Depends(get_ws_manager),
# ) -> dict:
#     return {
#         project_id: {
#             interview_id: [
#                 interview["message_count"],
#                 list(interview["users"]),
#             ]
#             for interview_id, interview in interviews.items()
#         }
#         for project_id, interviews in manager.active_connections.items()
#     }
#
#
# @router.get("/connect/{project_id}/{interview_id}")
# async def connect(
#     project_id: UUID4,
#     interview_id: UUID4,
#     db: DBSession,
#     jwt: AdminToken,
# ):
#     token = create_interview_token(
#         project_id=project_id,
#         interview_id=interview_id,
#         interviewer=Interviewer.HUMAN,
#     )
#     url = f"/interview?token={token}"
#     return RedirectResponse(url=url)
#
#
# @router.post("/broadcast")
# async def broadcast(
#     jwt: AdminToken,
#     broadcast: BroadcastRequest,
#     manager: WebSocketConnectionManager = Depends(get_ws_manager),
# ):
#     payload = {
#         "type": "message",
#         "content": broadcast.message,
#         "message_id": None,
#         "interview_id": None,
#         "role": "interviewer",
#     }
#     await manager.broadcast_message(payload)
#
#
# @router.websocket("/chat")
# async def human_interview_websocket_endpoint(
#     db: DBSession,
#     websocket: WebSocket,
#     manager: WebSocketConnectionManager = Depends(get_ws_manager),
# ):
#     """Endpoint for human-to-human chat"""
#     # FIXME:
#     # This is outdated, at least needs to
#     # - change the IDs to UUID4
#     # - add data validation
#
#     token = websocket.cookies.get("interview_token")
#     if not token:
#         raise WebSocketException(401, "Unauthorized")
#     try:
#         jwt = decode_jwt(token)
#     except JWTError as e:
#         raise WebSocketException(401, str(e))
#
#     project_id = jwt.get("project_id", 1)
#     if interview_id := jwt.get("interview_id"):
#         interview = db.interviews.get_interview(
#             project_id=project_id,
#             interview_id=interview_id,
#             full=True,
#         )
#         interview_history = interview.messages
#         if interview_history:
#             message_id = interview.messages[-1].message_id
#         else:
#             message_id = 0
#     else:
#         raise WebSocketException(400, "Interview ID not provided")
#
#     role = (
#         MessageRole.USER
#         if jwt.get("scope", "user") == "guest"
#         else MessageRole.ASSISTANT
#     )
#
#     project_id, interview_id = int(project_id), int(interview_id)
#
#     await manager.connect(websocket, project_id, interview_id, role)
#
#     if interview_history:
#         # FIXME: Doesn't work for Human interviewer chat
#         replay_history(interview_history, interview_id)
#
#     try:
#         async for data in websocket.iter_json():
#             # TODO: Add data validation
#             manager.active_connections[project_id][interview_id]["message_count"] += 1
#             message_id = manager.active_connections[project_id][interview_id][
#                 "message_count"
#             ]
#
#             payload = {
#                 "message_id": message_id,
#                 "interview_id": interview_id,
#                 "role": role,
#             } | data
#
#             interview_id, message_id = db.interviews.insert_message(
#                 message_id,
#                 content=data["content"],
#                 role=role,
#                 interview_id=interview_id,
#                 project_id=project_id,
#             )
#
#             # Send the message to the recipient if they're connected
#             send_to: MessageRole = (
#                 MessageRole.ASSISTANT if role == "user" else MessageRole.USER
#             )
#
#             await manager.send_personal_message(
#                 payload, project_id, interview_id, send_to
#             )
#     except WebSocketDisconnect:
#         pass
#     except Exception as e:
#         print(f"Unexpected error: {e}")
#     finally:
#         await manager.disconnect(project_id, interview_id, role)
