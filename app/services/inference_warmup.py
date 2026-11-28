"""Starting the self-hosted model before a respondent needs it.

The self-hosted model runs on EC2 instances the proxy stops when idle, and
one takes about two minutes to boot and load its weights. Left until the
first call -- the respondent's first answer -- that is two minutes of them
looking at a waiting notice. Asked when they open the interview, the boot
overlaps the consent, the welcome screen and the first question, and most
never wait at all.

`warm_up` asks the proxy's `POST /v1/availability`, which starts an instance
if none is up and touches nothing else: it is not a request served, so it
keeps no instance from being stopped as idle. Called from the respondent
page's preflight (`validate_interview_params`), from `create_interview` and
from the interview websocket, so a test (no preflight) and a resumed
interview are covered too. In the background, never on the request's time,
and at most once every `WARM_UP_INTERVAL` per process: the proxy only needs
to hear it once to start a box.
"""

import asyncio
import time

import httpx
from uvicorn.config import logger

from ainterviewer.agents.config import AgentConfigs
from ainterviewer.lpm.clients import is_self_hosted
from ainterviewer.settings import settings as lib_settings

from ..distribution import runs_on_answers
from ..settings import app_settings

#: Seconds between two warm-ups from this process.
WARM_UP_INTERVAL = 30.0

#: The proxy may ask AWS to start an instance before it answers.
TIMEOUT = httpx.Timeout(30.0, connect=5.0)

_last_warm_up: float | None = None
# Held so a task is not garbage collected mid-flight.
_pending: set[asyncio.Task] = set()


def needs_self_hosted(agent_configs: AgentConfigs) -> bool:
    """Whether an interview on `agent_configs` sends anything to the
    self-hosted model: any agent that runs on answers, on a model with no
    hosted provider's prefix."""
    return any(
        is_self_hosted(agent.model)
        for name in AgentConfigs.model_fields
        if runs_on_answers(name, (agent := getattr(agent_configs, name)).include)
    )


def warm_up(agent_configs: AgentConfigs | None) -> None:
    """Have the proxy start the self-hosted model, in the background, if an
    interview on `agent_configs` will use it and this process has not asked
    in the last `WARM_UP_INTERVAL` seconds."""
    global _last_warm_up

    if agent_configs is None or not app_settings.services.inference.warm_up:
        return
    if not needs_self_hosted(agent_configs):
        return

    now = time.monotonic()
    if _last_warm_up is not None and now - _last_warm_up < WARM_UP_INTERVAL:
        return
    _last_warm_up = now

    task = asyncio.create_task(ask_availability())
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def ask_availability() -> None:
    """Ask the proxy to make the self-hosted model available, and log the
    answer. Never raises: a warm-up that fails costs nothing but the head
    start, and the interview's own calls say what is wrong."""
    url = f"{lib_settings.llm.vllm_api_base}/availability"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post(url)
    except httpx.HTTPError as e:
        logger.warning("Warming up the self-hosted model failed: %r", e)
        return

    if response.status_code == 404:
        # Not our proxy -- vLLM itself, say, which has nothing to start.
        logger.debug("%s has no availability route; nothing to warm up", url)
        return
    try:
        response.raise_for_status()
        availability = response.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("Warming up the self-hosted model failed: %r", e)
        return

    if availability.get("ready"):
        logger.debug("Self-hosted model is up")
    else:
        logger.info(
            "Self-hosted model is not up yet (%s, retry after %s): %s",
            availability.get("code"),
            availability.get("retry_after"),
            availability.get("detail"),
        )
