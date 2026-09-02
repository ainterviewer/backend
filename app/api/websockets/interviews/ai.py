# FIXME:
# - Do we need different cookies for the AI and Human interviews?
# - Handle errors, e.g. when security agent raises an error

# NOTE:
# - Consider changing to server side events instead of websockets if it
# improves unstable connections

import asyncio
from typing import Literal

from any_llm.exceptions import (
    AnyLLMError,
    AuthenticationError,
    ContentFilterError,
    ContextLengthExceededError,
    GatewayTimeoutError,
    InsufficientFundsError,
    InvalidRequestError,
    MissingApiKeyError,
    ModelNotFoundError,
    ProviderError,
    RateLimitError,
    UnsupportedParameterError,
    UnsupportedProviderError,
    UpstreamProviderError,
)
from fastapi import (
    APIRouter,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from jinja2 import ChoiceLoader, DictLoader, PackageLoader
from sqlalchemy.exc import NoResultFound
from uvicorn.config import logger

from ainterviewer.interfaces import OutgoingData
from ainterviewer.interview import AInterviewer
from ainterviewer.lpm.types import CustomToken

from ....db import InterviewDataBase
from ....dependencies import DBSession
from ....embed.client import embedding_client
from ....embed.queue import QueueEmbedder, chunk_queue
from ....utils import replay_history
from ..auth import authenticate_or_close
from ..handler import WebsocketMessageHandler
from ..manager import session_manager

router = APIRouter(prefix="/ws", tags=["interviews"])


class RestartInterview(Exception):
    pass


# What the respondent is told, and therefore whether the frontend retries. The
# wire vocabulary is `OutgoingData.error`, so the two codes below carry the
# whole distinction: wait and we will try again, or this interview is over.
InterviewError = Literal["InstanceInitializing", "InferenceError"]

# Passes on its own: the provider is busy, briefly unreachable, or still coming
# up. ProviderError is any-llm's catch-all for 5xx, timeouts and connection
# failures, which belong here for the same reason.
_TRANSIENT_ERRORS = (
    RateLimitError,
    GatewayTimeoutError,
    UpstreamProviderError,
    ProviderError,
)

# Rooted in how the project or the deployment is configured, or in the request
# itself. Retrying reproduces it exactly, so the interview stops instead.
_FATAL_ERRORS = (
    ModelNotFoundError,
    AuthenticationError,
    MissingApiKeyError,
    UnsupportedProviderError,
    UnsupportedParameterError,
    InsufficientFundsError,
    ContextLengthExceededError,
    ContentFilterError,
    InvalidRequestError,
)


def _classify(exc: Exception) -> InterviewError:
    """Decide whether an interview failure is worth waiting out.

    Relies on any-llm's unified exception types, which `app.settings` turns on
    process-wide. Anything that is not an AnyLLMError at all came from our own
    code rather than from a provider, and is treated as fatal: a bug does not
    become less of one on the second attempt.
    """
    if "EC2 instance initializing" in str(exc):
        return "InstanceInitializing"

    if isinstance(exc, _FATAL_ERRORS):
        return "InferenceError"

    if isinstance(exc, _TRANSIENT_ERRORS):
        return "InstanceInitializing"

    return "InferenceError"


def _log_failure(exc: Exception, error: InterviewError, interview_id) -> None:
    """Say what happened in the detail the person fixing it will need."""
    if isinstance(exc, ModelNotFoundError):
        # Nearly always a project pointing at a model the provider has retired.
        logger.error(
            "Interview %s cannot run: the configured model was rejected by the provider (%s)",
            interview_id,
            exc,
        )
    elif isinstance(exc, (AuthenticationError, MissingApiKeyError)):
        logger.error(
            "Interview %s cannot run: provider %s rejected our credentials (%s)",
            interview_id,
            getattr(exc, "provider_name", "unknown"),
            exc,
        )
    elif isinstance(exc, AnyLLMError):
        # Provider-side and already classified; the type and status say enough
        # without a traceback through library frames.
        logger.warning(
            "Interview %s hit a %s from provider %s (status %s): %s -- reported as %s",
            interview_id,
            type(exc).__name__,
            getattr(exc, "provider_name", "unknown"),
            getattr(exc, "status_code", None),
            exc,
            error,
        )
    else:
        logger.exception("Unhandled error in interview %s", interview_id)


@router.websocket("/ai")
async def ai_interview_websocket_endpoint(
    *,
    websocket: WebSocket,
    db: DBSession,
    initialized: bool = Query(False),
):
    interview_token = await authenticate_or_close(websocket)
    if interview_token is None:
        return

    project_id = interview_token.project_id
    interview_id = interview_token.interview_id

    session_done = await session_manager.claim(project_id, interview_id)

    try:
        await _run_interview(
            websocket=websocket,
            db=db,
            project_id=project_id,
            interview_id=interview_id,
            initialized=initialized,
        )
    except asyncio.CancelledError:
        # Deliberately cancelled by session_manager because a newer connection
        # took over this interview. Close the socket and exit cleanly so
        # uvicorn doesn't log it as an unhandled ASGI exception.
        try:
            await websocket.close()
        except Exception:
            logger.debug("Websocket already closed while cancelling interview")
    finally:
        session_manager.release(project_id, interview_id, session_done)


async def _run_interview(
    *,
    websocket: WebSocket,
    db: InterviewDataBase,
    project_id,
    interview_id,
    initialized: bool,
):
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
                content=CustomToken.restart_interview,
            ).model_dump()
        )
        return

    project = db.projects.get_project(project_id)

    interview_config = project.config

    if (language := websocket.cookies.get("language")) is None:
        language = db.projects.get_default_language(project_id)

    project_localization = db.projects.get_project_localization(project_id, language)

    if interview_history:
        if initialized:
            last_message = interview_history[-1]
            continue_from_history = not (
                last_message.role == "assistant"
                and last_message.content == CustomToken.end_of_interview
            )
        else:
            messages, continue_from_history = replay_history(
                interview_history=interview_history,
                project_id=project_id,
                interview_id=interview_id,
            )

            for message in messages:
                await websocket.send_json(message.model_dump())

        if not continue_from_history:
            await websocket.close()
            return

    # Overrides shadow the package templates; anything absent falls through, so
    # a template added to the library can never go missing for an old project.
    prompt_loader = ChoiceLoader(
        [
            DictLoader(project_localization.prompt_overrides),
            PackageLoader("ainterviewer.agents.prompts.templates", "EN"),
        ]
    )

    external_params = db.projects.get_external_param_values_for_interview(interview_id)

    wmh = WebsocketMessageHandler(websocket, project_id, interview_id)

    # None when embedding is disabled or unconfigured, which makes the interview
    # loop emit no chunks at all.
    embedder = QueueEmbedder(chunk_queue) if embedding_client.enabled else None

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
            embedder=embedder,
        ) as interviewer:
            await interviewer.interview(interview_history=interview_history)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        # Every failure has to reach the client as a frame. An exception that
        # escapes here closes the socket with nothing on it, which the frontend
        # cannot tell apart from a dropped connection: it reconnects, the stored
        # history replays, the same failure repeats, and the respondent sits in
        # an endless "reconnecting" with no error. So classify, report, and let
        # the socket close cleanly -- the detail stays in the log, where the
        # person who can act on it will look.
        error = _classify(e)
        _log_failure(e, error, interview_id)

        try:
            await websocket.send_json(OutgoingData(error=error).model_dump())
        except Exception:
            logger.debug("Interview failure could not be reported: websocket closed")
