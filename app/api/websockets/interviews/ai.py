# FIXME:
# - Do we need different cookies for the AI and Human interviews?
# - Handle errors, e.g. when security agent raises an error

# NOTE:
# - Consider changing to server side events instead of websockets if it
# improves unstable connections

from fastapi import (
    APIRouter,
    Query,
    WebSocket,
    WebSocketDisconnect,
    WebSocketException,
)
from jinja2 import DictLoader
from jose import JWTError
from sqlalchemy.exc import NoResultFound
from uvicorn.config import logger

from ainterviewer.interfaces import OutgoingData
from ainterviewer.interview import AInterviewer
from ainterviewer.lpm.types import CustomTokens
from ainterviewer.settings import settings

from ....auth import decode_interview_token
from ....dependencies import DBSession
from ....utils import replay_history
from ..handler import WebsocketMessageHandler

router = APIRouter(prefix="/ws", tags=["interviews"])


class RestartInterview(Exception):
    pass


@router.websocket("/ai")
async def ai_interview_websocket_endpoint(
    *,
    websocket: WebSocket,
    db: DBSession,
    initialized: bool = Query(False),
):
    token = websocket.cookies.get("interview_token")

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

        if interview.interview_guide is None:
            raise ValueError("interview guide is not set")

        interview_history = interview.messages

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
            continue_from_history = not (
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

    agent_prompts = project_localization.prompts
    prompt_loader = DictLoader(agent_prompts.dump_templates())

    if settings.debug:
        pass
        # agent_prompts.print_prompts()

    external_params = db.projects.get_external_param_values_for_interview(interview_id)

    wmh = WebsocketMessageHandler(websocket, project_id, interview_id)

    try:
        async with AInterviewer(
            io=wmh,
            db=db,
            interview_guide=interview.interview_guide,
            config=interview_config,
            agent_configs=project_localization.agent_configs,
            template_loader=prompt_loader,
            project_id=project_id,
            interview_id=interview_id,
            previous_time_spent=interview.total_time_spent,
            language=language,
            referable_values=external_params,
        ) as interviewer:
            try:
                await interviewer.interview(interview_history=interview_history)
            except WebSocketDisconnect:
                pass
            except Exception as e:
                # TODO: Add more errors or handle it in a different way.
                if "EC2 instance initializing" in str(e):
                    logger.warning(
                        "Interview started with local LLM as model, but inference server is not available."
                    )

                    await websocket.send_json(
                        OutgoingData(error="InstanceInitializing").model_dump()
                    )
                else:
                    if str(e) == "Internal Server Error":
                        logger.error(
                            "Error fetching VLLM provider from inference proxy."
                        )
                        await websocket.send_json(
                            OutgoingData(error="InferenceError").model_dump()
                        )
                    else:
                        raise e
    except WebSocketDisconnect:
        pass
