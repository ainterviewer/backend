"""
This module help create synthetic interviews.
"""

# TODO:
# - Add language option and implement in rest of module
# - Handle reconnecting and replay history
import asyncio
import json
import platform
import random
from typing import Any, AsyncGenerator, Optional

import aiohttp
from pydantic import UUID4
from websockets import ClientConnection
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosedOK

import ainterviewer
from ainterviewer.agents import AnsweringAgent
from ainterviewer.interfaces import OutgoingData, OutgoingMessage, ReceivedData
from ainterviewer.lpm.clients import chat
from ainterviewer.lpm.types import CustomTokens
from ainterviewer.settings import settings
from ainterviewer.synthesize.interviewees import (
    BackgroundInfoOptions,
    generate_synthetic_persons,
)
from ainterviewer.types import LanguageCode, TestType
from ainterviewer.utils import get_function_signature_as_query_params

from ..settings import app_settings


async def fetch_token(
    interview_id: str,
    lang: Optional[LanguageCode] = None,
    test_type: TestType | None = None,
    synthetic: bool = True,
) -> str:
    "function args are automatically converted to query params"
    func_locals = locals()
    interview_id = func_locals.pop("interview_id")
    language = func_locals.pop("lang")

    query_params = get_function_signature_as_query_params(fetch_token, func_locals)

    url = f"http://{settings.app.ainterviewer_host}/api/projects/{interview_id}/interviews?{query_params}"

    cookies = {"language": language}
    headers = {
        "User-Agent": f"ainterviewer/{ainterviewer.__version__} ({platform.system()} {platform.release()}; Python/{platform.python_version()})"
    }

    async with aiohttp.ClientSession(cookies=cookies, headers=headers) as session:
        async with session.post(url) as response:
            response.raise_for_status()

            token = await response.text()

    return token


async def _connect_and_yield_messages(
    project_id: str,
    language: Optional[LanguageCode] = None,
    test_type: Optional[TestType] = None,
) -> AsyncGenerator[tuple[dict, ClientConnection], None]:
    """Connect to the websocket and yield parsed messages."""
    token = await fetch_token(project_id, language, test_type)
    cookies = f"interview_token={token}; language={language}"

    async with connect(
        f"ws://{app_settings.app.endpoint}/ws/ai",
        additional_headers=[("Cookie", cookies)],
    ) as websocket:
        try:
            async for message in websocket:
                json_data = json.loads(message)
                yield json_data, websocket
        except ConnectionClosedOK:
            print("Connection closed")


async def run_synthetic_answering_agent(
    agent: AnsweringAgent,
    project_id: str,
    language: Optional[LanguageCode] = None,
    delay_before_answer: Optional[tuple[float, float]] = None,
):
    async for json_data, websocket in _connect_and_yield_messages(project_id, language):
        match json_data["type"]:
            case "data":
                data = OutgoingData(**json_data)  # for validation purposes
                if interview_id := data.interview_id:
                    await add_interviewee(
                        project_id=project_id,
                        interview_id=interview_id,
                        interviewee=agent.interview_subject.model_dump(mode="json"),
                    )

            case "message":
                message = OutgoingMessage(**json_data)
                content = json_data["content"]
                if not message.can_answer:
                    agent.messages.append({"role": "user", "content": content})
                else:
                    response_text = await agent.answer(content)
                    await _send_response(websocket, response_text, delay_before_answer)


async def run_synthetic_fixed_answers(
    project_id: str,
    fixed_answers: list[str],
    language: Optional[LanguageCode] = None,
    delay_before_answer: Optional[tuple[float, float]] = None,
):
    # NOTE: We need to copy the list to avoid modifying the original which may be used
    # other places.
    fixed_answers = fixed_answers.copy()

    i = 0

    async for json_data, websocket in _connect_and_yield_messages(
        project_id, language, TestType.FIXED_ANSWERS
    ):
        match json_data["type"]:
            case "message":
                message = OutgoingMessage(**json_data)
                if message.can_answer:
                    i += 1
                    # every second question is a probe without any fixed
                    # answer, so we automatically skip that
                    if i % 2:
                        response_text = fixed_answers.pop(0)
                        delay = delay_before_answer
                    else:
                        response_text = CustomTokens.skip_question
                        delay = None

                    await _send_response(websocket, response_text, delay)


async def _send_response(
    websocket,
    response_text: str,
    delay_before_answer: Optional[tuple[float, float]] = None,
):
    """Send a response with optional delay."""
    if delay_before_answer:
        sleep_time = random.uniform(
            delay_before_answer[0] - delay_before_answer[1],
            delay_before_answer[0] + delay_before_answer[1],
        )

        await asyncio.sleep(sleep_time)

    await websocket.send(
        ReceivedData(type="message", content=response_text).model_dump_json()
    )


async def add_interviewee(
    project_id: str | UUID4, interview_id: str | UUID4, interviewee: dict[str, Any]
):
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"http://{app_settings.app.endpoint}/api/interviewee",
            json={
                "project_id": project_id,
                "interview_id": interview_id,
                "interview_subject": interviewee,
            },
        ) as response:
            response.raise_for_status()


async def run_synthesis_job_shuffled_ai(
    project_id: str,
    background_info_options: BackgroundInfoOptions,
    n_interviews: int,
    answering_model: str,
    language: LanguageCode,
    delay_before_answer: Optional[tuple[float, float]] = None,
):
    subjects = generate_synthetic_persons(background_info_options, n_interviews)

    # Create agents
    agents = []
    for subject in subjects:
        agent = AnsweringAgent(
            model=answering_model,
            chat_api=lambda messages, **kwargs: chat(
                messages, model=answering_model, **kwargs
            ),
            interview_subject=subject,
            language=language if language != "EN" else None,
        )
        agents.append(agent)

    # Run agents concurrently but with a small delay between starts
    tasks = []
    for agent in agents:
        task = asyncio.create_task(
            run_synthetic_answering_agent(
                agent=agent,
                project_id=project_id,
                language=language,
                delay_before_answer=delay_before_answer,
            )
        )
        tasks.append(task)
        await asyncio.sleep(1)  # Small delay between starting agents

    # Wait for all agents to complete
    results = await asyncio.gather(*tasks, return_exceptions=True)

    return results


async def run_synthesis_job_fixed_answers(
    project_id: str,
    fixed_answers: list[str],
    n_interviews: int,
    language: LanguageCode,
    delay_before_answer: Optional[tuple[float, float]] = None,
):
    tasks = []
    for _ in range(n_interviews):
        task = asyncio.create_task(
            run_synthetic_fixed_answers(
                project_id=project_id,
                fixed_answers=fixed_answers,
                language=language,
                delay_before_answer=delay_before_answer,
            )
        )
        tasks.append(task)
        await asyncio.sleep(1)  # Small delay between starting agents

    # Wait for all agents to complete
    results = await asyncio.gather(*tasks, return_exceptions=True)

    return results
