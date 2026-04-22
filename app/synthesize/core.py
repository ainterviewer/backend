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
from websockets import ClientConnection, ConnectionClosedError
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosedOK

import ainterviewer
from ainterviewer.agents import AnsweringAgent
from ainterviewer.interfaces import OutgoingData, OutgoingMessage, ReceivedData
from ainterviewer.lpm.types import CustomTokens
from ainterviewer.synthesize.interviewees import (
    BackgroundInfoOptions,
    InterviewSubject,
    generate_synthetic_persons,
)
from ainterviewer.types import LanguageCode, MessageRole

from ..api.request_models import CreateInterviewRequest
from ..auth import InterviewToken
from ..db.types import InterviewType
from ..settings import app_settings


async def fetch_token(
    auth_token: str,
    project_id: str,
    test_run_id: str,
    language: Optional[LanguageCode] = None,
) -> InterviewToken:
    url = f"http://{app_settings.app.api_endpoint}/api/projects/{project_id}/{language}/interviews"

    cookies = {"access_token": auth_token}
    if language:
        cookies["language"] = language

    headers = {
        "User-Agent": f"ainterviewer/{ainterviewer.__version__} ({platform.system()} {platform.release()}; Python/{platform.python_version()})"
    }

    payload = CreateInterviewRequest(
        interview_type=InterviewType.SYNTHETIC_TEST,
        test_run_id=test_run_id,  # ty:ignore[invalid-argument-type]
    )

    async with aiohttp.ClientSession(cookies=cookies, headers=headers) as session:
        async with session.post(url, json=payload.model_dump(mode="json")) as response:
            print(await response.json())
            response.raise_for_status()

            token = (await response.text()).strip().strip('"')

    return InterviewToken.decode(token)


async def _connect_and_yield_messages(
    token: str,
    language: Optional[LanguageCode] = None,
) -> AsyncGenerator[tuple[dict, ClientConnection], None]:
    """Connect to the websocket and yield parsed messages."""

    cookies = f"interview_token={token}; language={language}"

    async with connect(
        f"ws://{app_settings.app.api_endpoint}/ws/ai",
        additional_headers=[("Cookie", cookies)],
    ) as websocket:
        try:
            async for message in websocket:
                json_data = json.loads(message)
                yield json_data, websocket
        except ConnectionClosedOK:
            print("Connection closed")
        except ConnectionClosedError as e:
            print(e)


async def run_synthetic_answering_agent(
    agent: AnsweringAgent,
    user_token: str,
    project_id: str,
    test_run_id: str,
    language: Optional[LanguageCode] = None,
    delay_before_answer: Optional[tuple[float, float]] = None,
):
    interview_token = await fetch_token(
        auth_token=user_token,
        project_id=project_id,
        test_run_id=test_run_id,
        language=language,
    )

    if isinstance(agent.interview_subject, InterviewSubject):
        interviewee: dict[str, Any] = agent.interview_subject.model_dump(mode="json")
    else:
        interviewee = agent.interview_subject

    await add_interviewee(
        user_token=user_token,
        project_id=project_id,
        interview_id=interview_token.interview_id,
        interviewee=interviewee,
    )

    async for json_data, websocket in _connect_and_yield_messages(
        interview_token.encode(), language
    ):
        match json_data["type"]:
            case "data":
                _ = OutgoingData(**json_data)  # for validation purposes

            case "message":
                message = OutgoingMessage(**json_data)
                content = json_data["content"]
                if not message.can_answer:
                    agent.messages.append(
                        {"role": MessageRole.USER, "content": content}
                    )
                else:
                    response_text = await agent.answer(
                        content, survey_item=message.survey_item
                    )
                    await _send_response(websocket, response_text, delay_before_answer)


async def run_synthetic_fixed_answers(
    user_token: str,
    project_id: str,
    test_run_id: str,
    fixed_answers: list[str],
    language: Optional[LanguageCode] = None,
    delay_before_answer: Optional[tuple[float, float]] = None,
):
    # NOTE: We need to copy the list to avoid modifying the original which may be used
    # other places.
    fixed_answers = fixed_answers.copy()

    i = 0

    interview_token = await fetch_token(
        auth_token=user_token,
        project_id=project_id,
        test_run_id=test_run_id,
        language=language,
    )

    async for json_data, websocket in _connect_and_yield_messages(
        interview_token.encode(), language
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
    user_token: str,
    project_id: str | UUID4,
    interview_id: str | UUID4,
    interviewee: dict[str, Any] | str,
):
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"http://{app_settings.app.api_endpoint}/api/projects/{project_id}/interviewee",
            json={
                "interview_id": str(interview_id),
                "interview_subject": interviewee,
            },
            cookies={"access_token": user_token},
        ) as response:
            response.raise_for_status()


async def run_synthesis_job_fixed_answers(
    project_id: str,
    test_run_id: str,
    user_token: str,
    fixed_answers: list[str],
    n_interviews: int,
    language: LanguageCode,
    delay_before_answer: Optional[tuple[float, float]] = None,
):
    tasks = []
    for _ in range(n_interviews):
        task = asyncio.create_task(
            run_synthetic_fixed_answers(
                user_token=user_token,
                project_id=project_id,
                test_run_id=test_run_id,
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


async def run_synthesis_job_fixed_ai(
    project_id: str,
    test_run_id: str,
    user_token: str,
    fixed_personas: list[str],
    n_interviews: int,
    answering_model: str,
    language: LanguageCode,
    delay_before_answer: Optional[tuple[float, float]] = None,
):
    shuffled_personas = [random.choice(fixed_personas) for _ in range(n_interviews)]

    # Create agents
    agents = []
    for persona in shuffled_personas:
        agent = AnsweringAgent(
            model=answering_model,
            interview_subject=persona,
            language=language,
        )
        agents.append(agent)

    # Run agents concurrently but with a small delay between starts
    tasks = []
    for agent in agents:
        task = asyncio.create_task(
            run_synthetic_answering_agent(
                agent=agent,
                user_token=user_token,
                project_id=project_id,
                test_run_id=test_run_id,
                language=language,
                delay_before_answer=delay_before_answer,
            )
        )
        tasks.append(task)
        await asyncio.sleep(1)  # Small delay between starting agents

    # Wait for all agents to complete
    results = await asyncio.gather(*tasks, return_exceptions=True)

    return results


async def run_synthesis_job_shuffled_ai(
    project_id: str,
    test_run_id: str,
    user_token: str,
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
            interview_subject=subject,
            language=language,
        )
        agents.append(agent)

    # Run agents concurrently but with a small delay between starts
    tasks = []
    for agent in agents:
        task = asyncio.create_task(
            run_synthetic_answering_agent(
                agent=agent,
                user_token=user_token,
                project_id=project_id,
                test_run_id=test_run_id,
                language=language,
                delay_before_answer=delay_before_answer,
            )
        )
        tasks.append(task)
        await asyncio.sleep(1)  # Small delay between starting agents

    # Wait for all agents to complete
    results = await asyncio.gather(*tasks, return_exceptions=True)

    return results
