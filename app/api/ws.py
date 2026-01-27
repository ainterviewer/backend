# FIXME:
# - Do we need different cookies for the AI and chat interviews?
# - Handle errors, e.g. when security agent raises an error

# NOTE:
# - Consider changing to server side events instead of websockets if it
# improves unstable connections
from fastapi.websockets import WebSocketState

import json

from fastapi import (
    APIRouter,
    Depends,
    Query,
    WebSocket,
    WebSocketDisconnect,
    WebSocketException,
)
from fastapi.responses import RedirectResponse
from jinja2 import DictLoader
from jose import JWTError
from pydantic import UUID4
from sqlalchemy.exc import NoResultFound

from ainterviewer.interfaces import OutgoingData
from ainterviewer.interview import AInterviewer
from ainterviewer.lpm.types import CustomTokens
from ainterviewer.settings import settings
from ainterviewer.types import Interviewer, MessageRole

from ..auth import create_interview_token, decode_interview_token, decode_jwt
from ..dependencies import (
    AdminToken,
    DBSession,
    get_ws_manager,
)
from ..utils import replay_history
from ..websockets import WebSocketConnectionManager, WebsocketMessageHandler
from .models import Broadcast

router = APIRouter(prefix="/ws", tags=["interviews"])


class RestartInterview(Exception):
    pass


@router.get("/connections")
async def active_websockets(
    jwt: AdminToken,
    manager: WebSocketConnectionManager = Depends(get_ws_manager),
) -> dict:
    return {
        project_id: {
            interview_id: [
                interview["message_count"],
                list(interview["users"]),
            ]
            for interview_id, interview in interviews.items()
        }
        for project_id, interviews in manager.active_connections.items()
    }


@router.get("/connect/{project_id}/{interview_id}")
async def connect(
    project_id: UUID4,
    interview_id: UUID4,
    db: DBSession,
    jwt: AdminToken,
):
    token = create_interview_token(
        project_id=project_id,
        interview_id=interview_id,
        interviewer=Interviewer.HUMAN,
    )
    url = f"/interview?token={token}"
    return RedirectResponse(url=url)


@router.post("/broadcast")
async def broadcast(
    jwt: AdminToken,
    broadcast: Broadcast,
    manager: WebSocketConnectionManager = Depends(get_ws_manager),
):
    payload = {
        "type": "message",
        "content": broadcast.message,
        "message_id": None,
        "interview_id": None,
        "role": "interviewer",
    }
    await manager.broadcast_message(payload)


@router.websocket("/ai")
async def ai_websocket_endpoint(
    *,
    websocket: WebSocket,
    db: DBSession,
    initialized: bool = Query(False),
):
    # FIXME: Handle errors, e.g. when security agent raises an error
    token = websocket.cookies.get("interview_token")
    referer = websocket.cookies.get("referer")
    forward_params = websocket.cookies.get("forward_params")
    forward_params = json.loads(forward_params) if forward_params else {}

    if token is None:
        raise WebSocketException(401, "Unauthorized")
    try:
        interview_token = decode_interview_token(token)
    except JWTError as e:
        raise WebSocketException(401, str(e))

    await websocket.accept()

    project_id = interview_token.project_id
    interview_id = interview_token.interview_id

    # TODO: Implement a better way to handle interview restarts
    try:
        interview = db.interviews.get_interview(
            project_id=project_id,
            interview_id=interview_id,
            full=True,
        )

        interview_history = interview.messages
        if interview_history:
            message_id = interview.messages[-1].message_id
        else:
            message_id = 0

    except (NoResultFound, RestartInterview):
        print("creating new interview")
        await websocket.send_json(
            OutgoingData(
                content=CustomTokens.restart_interview,
            ).model_dump()
        )
        exit()

    project = db.projects.get_project(project_id)

    interview_config = project.config

    if (language := websocket.cookies.get("language")) is None:
        language = interview_config.default_language

    project_localization = db.projects.get_project_localization(project_id, language)

    if interview_history:
        if initialized:
            last_message = interview_history[-1]
            continue_from_history = (
                last_message.role == "assistant"
                and last_message.content == CustomTokens.end_of_interview
            )
        else:
            messages, continue_from_history = replay_history(
                interview_history=interview_history,
                project_id=project_id,
                interview_id=interview_id,
            )

            for messages in messages:
                await websocket.send_json(messages.model_dump())

            if not continue_from_history:
                await websocket.close()
                return

    try:
        agent_prompts = project_localization.prompts
        prompt_loader = DictLoader(agent_prompts.dump_templates())

        if settings.debug:
            pass
            # agent_prompts.print_prompts()

        wmh = WebsocketMessageHandler(websocket, project_id, interview_id)

        async with AInterviewer(
            io=wmh,
            db=db,
            interview_guide=project_localization.interview_guide,
            config=interview_config,
            agent_configs=project_localization.agent_configs,
            template_loader=prompt_loader,
            project_id=project_id,
            interview_id=interview_id,
            previous_time_spent=interview.total_time_spent,
            message_id=message_id,
            frontend_language=language,
            referable_values={
                "referer": referer,
                "test": forward_params.get("test"),
                "user1": forward_params.get("user1"),
            },
        ) as interviewer:
            try:
                await interviewer.interview(
                    probing="restricted",
                    interview_history=interview_history,
                )
            except Exception as e:
                # FIXME: Since the endpoint is now fetched via proxy, we need
                # to move this check somewhere else before the interview is
                # initialized.
                # TODO: Add more errors or handle it in a different
                # way.
                await websocket.send_json(
                    OutgoingData(error="InstanceInitializing").model_dump()
                )
                raise e
    except WebSocketDisconnect:
        pass
    finally:
        if not websocket.application_state == WebSocketState.DISCONNECTED:
            await websocket.close()


@router.websocket("/chat")
async def chat_websocket_endpoint(
    db: DBSession,
    websocket: WebSocket,
    manager: WebSocketConnectionManager = Depends(get_ws_manager),
):
    """Endpoint for human-to-human chat"""
    # FIXME:
    # This is outdated, at least needs to
    # - change the IDs to UUID4
    # - add data validation

    token = websocket.cookies.get("interview_token")
    if not token:
        raise WebSocketException(401, "Unauthorized")
    try:
        jwt = decode_jwt(token)
    except JWTError as e:
        raise WebSocketException(401, str(e))

    project_id = jwt.get("project_id", 1)
    if interview_id := jwt.get("interview_id"):
        interview = db.interviews.get_interview(
            project_id=project_id,
            interview_id=interview_id,
            full=True,
        )
        interview_history = interview.messages
        if interview_history:
            message_id = interview.messages[-1].message_id
        else:
            message_id = 0
    else:
        raise WebSocketException(400, "Interview ID not provided")

    role = (
        MessageRole.USER
        if jwt.get("scope", "user") == "guest"
        else MessageRole.ASSISTANT
    )

    project_id, interview_id = int(project_id), int(interview_id)

    await manager.connect(websocket, project_id, interview_id, role)

    if interview_history:
        # FIXME: Doesn't work for Human interviewer chat
        replay_history(interview_history, interview_id)

    try:
        async for data in websocket.iter_json():
            # TODO: Add data validation
            manager.active_connections[project_id][interview_id]["message_count"] += 1
            message_id = manager.active_connections[project_id][interview_id][
                "message_count"
            ]

            payload = {
                "message_id": message_id,
                "interview_id": interview_id,
                "role": role,
            } | data

            interview_id, message_id = db.interviews.insert_message(
                message_id,
                content=data["content"],
                role=role,
                interview_id=interview_id,
                project_id=project_id,
            )

            # Send the message to the recipient if they're connected
            send_to: MessageRole = (
                MessageRole.ASSISTANT if role == "user" else MessageRole.USER
            )

            await manager.send_personal_message(
                payload, project_id, interview_id, send_to
            )
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"Unexpected error: {e}")
    finally:
        await manager.disconnect(project_id, interview_id, role)
