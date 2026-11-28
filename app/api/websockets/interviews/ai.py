# FIXME:
# - Do we need different cookies for the AI and Human interviews?
# - Handle errors, e.g. when security agent raises an error

# NOTE:
# - Consider changing to server side events instead of websockets if it
# improves unstable connections

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

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
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from jinja2 import ChoiceLoader, DictLoader, PackageLoader
from sqlalchemy.exc import NoResultFound
from uvicorn.config import logger

from ainterviewer.agents.config import AgentConfigs
from ainterviewer.exceptions import ModelUnavailable, UnavailableReason
from ainterviewer.interfaces import OutgoingData
from ainterviewer.interview import AInterviewer
from ainterviewer.lpm.types import CustomToken

from ....db.crud import InterviewDataBase
from ....db.repositories.errors import InterviewGoneError, NotPublishedError
from ....db.repositories.project import ContentSource
from ....dependencies import DBSession
from ....embed.client import embedding_client
from ....embed.queue import QueueEmbedder, chunk_queue
from ....schemas.interviews import WaitingFrame
from ....services import inference_warmup
from ..auth import authenticate_or_close
from ..handler import WebsocketMessageHandler
from ..manager import session_manager
from .replay import replay_history, resume_message

#: Agents a test run's interviewer model leaves alone: the synthetic
#: interviewee, whose model the run sets apart, and the image model.
_KEEP_OWN_MODEL = frozenset({"answering", "visual"})


def with_interviewer_model(agent_configs: AgentConfigs, model: str) -> AgentConfigs:
    """`agent_configs` with every interview agent on `model`
    (`TestRunTable.interviewer_model`); temperatures and prompts stay."""
    configs = agent_configs.model_copy(deep=True)
    for name in AgentConfigs.model_fields:
        if name not in _KEEP_OWN_MODEL:
            getattr(configs, name).model = model
    return configs


router = APIRouter(prefix="/ws", tags=["interviews"])


class RestartInterview(Exception):
    pass


# Passes on its own: the provider is busy, briefly unreachable, or still coming
# up. ProviderError is any-llm's catch-all for 5xx, timeouts and connection
# failures, which belong here for the same reason. A provider that said why it
# cannot serve, and for how long, comes as `ModelUnavailable` instead (raised
# by the library's `chat`), and is waited on for as long as it said.
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

#: How long a respondent waits on a model the provider says is coming -- an
#: instance booting, a pool at capacity -- before they are told the interview
#: cannot continue. Over twice the self-hosted model's two-minute cold start.
EXPLAINED_WAIT = 300.0

#: The same for a failure nobody explained: a 5xx, a timeout, a dropped
#: connection. It may well never pass, so it gets about what the frontend's
#: reconnect cycle used to give it, and is tried again every
#: `UNEXPLAINED_RETRY_AFTER` seconds.
UNEXPLAINED_WAIT = 60.0
UNEXPLAINED_RETRY_AFTER = 15


@dataclass(frozen=True)
class Wait:
    """Try the turn again in `seconds`, while waiting has not passed `budget`."""

    reason: Literal["starting", "busy"]
    seconds: int
    budget: float


def wait_for(exc: Exception) -> Wait | None:
    """How long the failure `exc` is worth waiting out, or None if it is not.

    What the provider said decides, when it said anything: our proxy names a
    starting instance, a full pool, and a pool with nothing to start, and only
    the first two are worth waiting for. Without that, the type does: any-llm's
    unified errors (turned on process-wide by `app.settings`). Anything that is
    not one of theirs came from our own code, and a bug does not become less
    of one on the second attempt.
    """
    if isinstance(exc, ModelUnavailable):
        if exc.retry_after is None:
            return None
        reason = "starting" if exc.reason is UnavailableReason.STARTING else "busy"
        return Wait(reason, max(exc.retry_after, 1), EXPLAINED_WAIT)

    if isinstance(exc, _FATAL_ERRORS):
        return None

    if isinstance(exc, _TRANSIENT_ERRORS):
        return Wait("busy", UNEXPLAINED_RETRY_AFTER, UNEXPLAINED_WAIT)

    return None


class _StoredInterview(Protocol):
    @property
    def messages(self) -> Sequence[Any]: ...


async def _pause(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _now() -> float:
    return time.monotonic()


async def run_with_waits[I: _StoredInterview](
    interview: I,
    run: Callable[[I], Awaitable[None]],
    reload: Callable[[], I],
    send: Callable[[dict], Awaitable[Any]],
    interview_id,
) -> bool:
    """`run` the interview from what is stored, and run it again from what is
    stored then, for as long as its failures are worth waiting out.

    The respondent hears of a wait at once, as a `WaitingFrame` with the
    reason and the time to the next try, and keeps the socket: closing it to
    have the browser reconnect would replay the chat and look like a lost
    connection, which it is not. The turn that failed is run again from the
    stored messages, as a reconnect would.

    Waiting stops at the failure's budget, counted from the first failure
    since the interview last got further -- a respondent who waited through
    one cold start is not charged for it at the next. Then, or for a failure
    not worth waiting for, the respondent is told the interview cannot go on
    (`OutgoingData(error="InferenceError")`) and the socket closes cleanly.

    True once a run went to the end, False once the respondent was told it
    cannot. The respondent leaving and the interview being deleted are not
    failures: `WebSocketDisconnect` and `InterviewGoneError` pass through.
    """
    waiting_since: float | None = None
    stored = len(interview.messages)

    while True:
        try:
            await run(interview)
            return True
        except (WebSocketDisconnect, InterviewGoneError):
            raise
        except Exception as e:
            failure = e

        if (wait := wait_for(failure)) is None:
            await _report_failure(send, failure, interview_id)
            return False

        try:
            interview = reload()
        except Exception:
            logger.exception("Interview %s could not be reloaded", interview_id)
            await _report_failure(send, failure, interview_id)
            return False

        now = _now()
        if waiting_since is None or len(interview.messages) > stored:
            waiting_since = now
        stored = len(interview.messages)
        waited = now - waiting_since

        if waited + wait.seconds > wait.budget:
            logger.error(
                "Interview %s gave up after waiting %ds for its model: %s",
                interview_id,
                waited,
                failure,
            )
            await _report_failure(send, failure, interview_id)
            return False

        (logger.warning if waited == 0 else logger.info)(
            "Interview %s waiting %ss for its model (%s, %ds so far): %s",
            interview_id,
            wait.seconds,
            wait.reason,
            waited,
            failure,
        )
        try:
            await send(
                WaitingFrame(reason=wait.reason, retry_after=wait.seconds).model_dump(
                    mode="json"
                )
            )
        except Exception:
            # Nobody left to wait for: the respondent closed the page.
            logger.debug("Interview %s stopped waiting: websocket closed", interview_id)
            return False
        await _pause(wait.seconds)


async def _report_failure(
    send: Callable[[dict], Awaitable[Any]], exc: Exception, interview_id
) -> None:
    """Tell the respondent the interview cannot go on, and log why.

    Every failure has to reach the client as a frame. An exception that
    escapes the websocket closes it with nothing on it, which the frontend
    cannot tell apart from a dropped connection: it reconnects, the stored
    history replays, the same failure repeats, and the respondent sits in an
    endless "reconnecting" with no error. The detail stays in the log, where
    the person who can act on it will look.
    """
    _log_failure(exc, interview_id)
    try:
        await send(OutgoingData(error="InferenceError").model_dump())
    except Exception:
        logger.debug("Interview failure could not be reported: websocket closed")


def _log_failure(exc: Exception, interview_id) -> None:
    """Say what happened in the detail the person fixing it will need."""
    if isinstance(exc, ModelUnavailable):
        logger.error("Interview %s cannot run: %s", interview_id, exc)
    elif isinstance(exc, ModelNotFoundError):
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
            "Interview %s hit a %s from provider %s (status %s): %s",
            interview_id,
            type(exc).__name__,
            getattr(exc, "provider_name", "unknown"),
            getattr(exc, "status_code", None),
            exc,
        )
    else:
        logger.exception("Unhandled error in interview %s", interview_id)


async def _send_redirect(
    websocket: WebSocket, db: InterviewDataBase, project_id, interview_id
) -> None:
    """Tell the frontend where to send the respondent, after the end frame,
    when the project redirects how this interview ended. Nothing for an
    interview that has not ended: `interview()` also returns when it does not
    complete, and `completion_redirect` checks the stored status."""
    frame = db.interviews.completion_redirect(project_id, interview_id)
    if frame is not None:
        await websocket.send_json(frame.model_dump(mode="json"))


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
        # Deliberately cancelled by session_manager: either a newer connection
        # took over this interview, or the interview is being deleted. Close
        # the socket and exit cleanly so uvicorn doesn't log it as an unhandled
        # ASGI exception.
        try:
            if session_manager.ended_by_delete(project_id, interview_id, session_done):
                # The frame a reconnect to a deleted interview gets anyway (see
                # `_run_interview`), so the client starts over instead of
                # reconnecting into an interview that is no longer there.
                await websocket.send_json(
                    OutgoingData(content=CustomToken.restart_interview).model_dump()
                )
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

    # The interview's own language, fixed when it was created. Not a cookie:
    # nothing has set one since the server-rendered routes went, and falling
    # back to the project default ran Danish interviews with English agents.
    language = interview.language

    # The agent configs and prompt overrides of the version the interview
    # runs, so republishing mid-interview does not change how it is probed.
    # Interviews from before versions existed (and tests whose snapshot was
    # pruned) take the published version's, or the draft's if nothing is.
    # The guide itself lives on the interview row. `or_default` because they
    # are the same in every language: one removed since the interview began
    # must not stop it.
    source: ContentSource = interview.content_version_id or "published"
    try:
        project_localization = db.projects.get_project_localization(
            project_id, language, source=source, or_default=True
        )
    except NotPublishedError:
        project_localization = db.projects.get_project_localization(
            project_id, language, or_default=True
        )

    agent_configs = project_localization.agent_configs
    # A test run comparing models puts every interview agent on its model.
    if interviewer_model := db.tests.interviewer_model(interview_id):
        agent_configs = with_interviewer_model(agent_configs, interviewer_model)

    if interview_history:
        if initialized:
            last_message = interview_history[-1]
            continue_from_history = not (
                last_message.role == "assistant"
                and last_message.content == CustomToken.end_of_interview
            )
            # The client kept its chat, so it is not replayed, but it is told
            # whose turn it is: resend the message the interview waits on.
            if continue_from_history and (
                resumed := resume_message(interview_history, interview.interview_guide)
            ):
                await websocket.send_json(resumed.model_dump())
        else:
            messages, continue_from_history = replay_history(
                interview_history=interview_history,
                interview_guide=interview.interview_guide,
            )

            for message in messages:
                await websocket.send_json(message.model_dump())

        if not continue_from_history:
            # Finished already: a reload of the end screen still has to leave
            # for the panel, or the respondent is stuck on it.
            await _send_redirect(websocket, db, project_id, interview_id)
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

    # None when embedding is disabled, unconfigured or not live, which makes the
    # interview loop emit no chunks at all.
    embedder = QueueEmbedder(chunk_queue) if embedding_client.live else None

    # Booting the self-hosted model now, if it is down, overlaps the boot
    # with whatever the respondent reads before the first call: a test's
    # welcome screen, the question in front of someone resuming.
    inference_warmup.warm_up(agent_configs)

    async def run(interview) -> None:
        async with AInterviewer(
            io=wmh,
            db=db,
            interview_guide=interview.interview_guide,
            config=project_localization.config,
            agent_configs=agent_configs,
            template_loader=prompt_loader,
            project_id=project_id,
            interview_id=interview_id,
            previous_time_spent=interview.total_time_spent,
            language=language,
            referable_values=external_params,
            embedder=embedder,
        ) as interviewer:
            await interviewer.interview(interview_history=interview.messages)

    try:
        if await run_with_waits(
            interview,
            run,
            reload=lambda: db.interviews.get_interview(
                project_id=project_id, interview_id=interview_id, full=True
            ),
            send=websocket.send_json,
            interview_id=interview_id,
        ):
            await _send_redirect(websocket, db, project_id, interview_id)
    except WebSocketDisconnect:
        pass
    except InterviewGoneError:
        # Deleted while this session was running, and the delete did not end
        # the session first (see InterviewSessionManager.end) -- the foreign
        # key caught the write instead. Same frame as a reconnect to a deleted
        # interview, rather than a service failure the respondent cannot act on.
        logger.info(
            "Interview %s was deleted mid-session; restarting client", interview_id
        )
        try:
            await websocket.send_json(
                OutgoingData(content=CustomToken.restart_interview).model_dump()
            )
        except Exception:
            logger.debug("Restart could not be sent: websocket closed")
