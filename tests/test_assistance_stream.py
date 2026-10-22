"""The guide assistant's chat stream, driven offline.

`stream_messages` walks the agent graph node by node and turns streamed text
and tool results into newline-delimited `ChatMessage` frames; `to_chat_messages`
replays a stored history as the same frames. Nothing else exercises either, so
an upgrade of pydantic-ai that renames part of that surface (as 2.x did with
`AgentStream.timestamp`) would otherwise surface only in the browser.

The model is a `FunctionModel` and the lib's generators are stubbed, so no
request leaves the process. The history goes through `ModelMessagesTypeAdapter`
exactly as `AssistanceRepository` stores and reads it.
"""

import asyncio
import json
import uuid

import pytest
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from ainterviewer.interview_guides import InterviewGuide, Question, QuestionSection
from app.services.assistance import agent as assistance

QUESTION = Question(main_question="What brought you here?")
SECTION = QuestionSection(description="Background", questions=[QUESTION])


@pytest.fixture(autouse=True)
def stub_generators(monkeypatch):
    async def generate_question(*args, **kwargs):
        return QUESTION

    async def generate_section(*args, **kwargs):
        return SECTION

    monkeypatch.setattr(assistance, "generate_question", generate_question)
    monkeypatch.setattr(assistance, "generate_section", generate_section)


async def both_tools_then_text(messages, info: AgentInfo):
    """Call both tools in one turn, then answer in text once they returned."""
    if not any(part.part_kind == "tool-return" for m in messages for part in m.parts):
        yield {
            0: DeltaToolCall(
                name="create_new_question",
                json_args='{"instructions": "q"}',
                tool_call_id="c1",
            ),
            1: DeltaToolCall(
                name="create_new_section",
                json_args='{"instructions": "s"}',
                tool_call_id="c2",
            ),
        }
    else:
        for word in ("Here ", "you ", "go."):
            yield word


class FakeAssistanceRepository:
    def __init__(self):
        self.chunks: list[str] = []

    def get_messages(self, **kwargs):
        return [
            m
            for chunk in self.chunks
            for m in ModelMessagesTypeAdapter.validate_json(chunk)
        ]

    def add_messages(self, messages_json, **kwargs):
        if isinstance(messages_json, bytes):
            messages_json = messages_json.decode()
        self.chunks.append(messages_json)


class FakeDB:
    def __init__(self):
        self.assistance = FakeAssistanceRepository()


def run_chat(db: FakeDB, prompt: str = "Help me") -> list[dict]:
    async def collect():
        with assistance.assistance_agent.override(
            model=FunctionModel(stream_function=both_tools_then_text)
        ):
            return [
                json.loads(frame)
                async for frame in assistance.stream_messages(
                    prompt,
                    uuid.uuid4(),
                    uuid.uuid4(),
                    uuid.uuid4(),
                    db,  # ty:ignore[invalid-argument-type]
                    InterviewGuide(),
                    "Tess",
                )
            ]

    return asyncio.run(collect())


def test_stream_emits_prompt_tool_results_and_text():
    frames = run_chat(FakeDB())

    assert frames[0]["role"] == "user"
    assert frames[0]["content"] == "Help me"
    # Parallel tool results stream in the order the calls finish.
    by_type = {f["type"]: f for f in frames if f["type"] != "message"}
    assert by_type.keys() == {"question", "section"}
    assert by_type["question"]["content"] == QUESTION.model_dump_json()
    assert by_type["section"]["content"] == SECTION.model_dump_json()
    # Text arrives as the reply so far; the last frame holds all of it.
    assert frames[-1] == frames[-1] | {"role": "model", "type": "message"}
    assert frames[-1]["content"] == "Here you go."


def test_replay_shows_every_part_of_a_turn():
    """Both tool results of one turn replay, not only the first part."""
    db = FakeDB()
    live = run_chat(db)

    replayed = [
        json.loads(frame)
        for m in db.assistance.get_messages()
        for frame in assistance.to_chat_messages(m)
    ]

    assert [(f["role"], f["type"]) for f in replayed] == [
        ("user", "message"),
        ("model", "question"),
        ("model", "section"),
        ("model", "message"),
    ]
    assert replayed[-1]["content"] == "Here you go."
    # Stored tool content comes back as plain data; replay must send the
    # client exactly what the live stream did.
    live_items = {f["type"]: f["content"] for f in live if f["type"] != "message"}
    assert replayed[1]["content"] == live_items["question"]
    assert replayed[2]["content"] == live_items["section"]


def test_replay_hides_the_greeting_trigger():
    from pydantic_ai import ModelRequest, UserPromptPart

    trigger = ModelRequest(parts=[UserPromptPart(assistance._GREETING_TRIGGER)])

    assert assistance.to_chat_messages(trigger) == []
