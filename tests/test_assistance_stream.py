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
def generated(monkeypatch) -> list[dict]:
    """Stub the lib's generators, recording what each call was given."""
    calls: list[dict] = []

    async def generate_question(*args, **kwargs):
        calls.append({"tool": "question", **kwargs})
        return QUESTION

    async def generate_section(*args, **kwargs):
        calls.append({"tool": "section", **kwargs})
        return SECTION

    monkeypatch.setattr(assistance, "generate_question", generate_question)
    monkeypatch.setattr(assistance, "generate_section", generate_section)
    return calls


def tool_returned(messages) -> bool:
    return any(part.part_kind == "tool-return" for m in messages for part in m.parts)


async def both_tools_then_text(messages, info: AgentInfo):
    """Call both tools in one turn, then answer in text once they returned."""
    if not tool_returned(messages):
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


def chat(db: FakeDB, stream_function, guide: InterviewGuide, prompt: str):
    return assistance.stream_messages(
        prompt,
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        db,  # ty:ignore[invalid-argument-type]
        guide,
        "Tess",
    )


def run_chat(
    db: FakeDB,
    prompt: str = "Help me",
    stream_function=both_tools_then_text,
    guide: InterviewGuide | None = None,
) -> list[dict]:
    async def collect():
        with assistance.assistance_agent.override(
            model=FunctionModel(stream_function=stream_function)
        ):
            return [
                json.loads(frame)
                async for frame in chat(
                    db, stream_function, guide or InterviewGuide(), prompt
                )
            ]

    return asyncio.run(collect())


def stored_prompts(db: FakeDB) -> list[str]:
    return [
        part.content
        for m in db.assistance.get_messages()
        for part in m.parts
        if part.part_kind == "user-prompt"
    ]


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


def test_reply_frames_only_grow():
    """Each text frame is the reply so far, never a fragment of it."""
    frames = [f for f in run_chat(FakeDB())[1:] if f["type"] == "message"]

    texts = [f["content"] for f in frames]
    assert texts == sorted(texts, key=len)
    assert all("Here you go.".startswith(t) for t in texts)
    assert len({f["timestamp"] for f in frames}) == 1


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


def test_replay_shows_the_greeting_but_not_its_trigger():
    messages, frame = assistance.greeting("Tess")

    replayed = [f for m in messages for f in assistance.to_chat_messages(m)]

    assert replayed == [frame]
    assert "Tess" in json.loads(frame)["content"]


def question_in(section_id: str):
    async def stream(messages, info: AgentInfo):
        if not tool_returned(messages):
            args = json.dumps({"instructions": "q", "section_id": section_id})
            yield {0: DeltaToolCall("create_new_question", args, tool_call_id="c1")}
        else:
            yield "Done."

    return stream


def test_question_tool_resolves_its_section_by_id(generated):
    other = QuestionSection(
        description="Other", questions=[Question(main_question="Anything else?")]
    )
    guide = InterviewGuide(question_sections=[other, SECTION])

    run_chat(FakeDB(), stream_function=question_in(str(SECTION.id)), guide=guide)

    assert [c["section"].id for c in generated] == [SECTION.id]


def test_unknown_section_id_is_sent_back_to_the_model(generated):
    calls = 0

    async def stream(messages, info: AgentInfo):
        nonlocal calls
        calls += 1
        if calls == 1:
            args = json.dumps({"instructions": "q", "section_id": str(uuid.uuid4())})
            yield {0: DeltaToolCall("create_new_question", args, tool_call_id="c1")}
        elif calls == 2:
            retry = messages[-1].parts[-1]
            assert retry.outcome == "retried"
            assert "no section with id" in retry.model_response_str()
            yield {
                0: DeltaToolCall(
                    "create_new_question", '{"instructions": "q"}', tool_call_id="c2"
                )
            }
        else:
            yield "Done."

    frames = run_chat(FakeDB(), stream_function=stream)

    assert [c["section"] for c in generated] == [None]
    # The failed call's error goes to the model, not to the chat.
    assert [f["type"] for f in frames].count("question") == 1


def test_a_failed_run_keeps_the_prompt():
    async def stream(messages, info: AgentInfo):
        yield "Partial "
        raise RuntimeError("provider went away")

    db = FakeDB()
    with pytest.raises(RuntimeError, match="provider went away"):
        run_chat(db, prompt="My topic", stream_function=stream)

    assert stored_prompts(db) == ["My topic"]


def test_a_client_that_leaves_keeps_the_prompt():
    db = FakeDB()

    async def leave_early():
        with assistance.assistance_agent.override(
            model=FunctionModel(stream_function=both_tools_then_text)
        ):
            stream = chat(db, both_tools_then_text, InterviewGuide(), "My topic")
            await anext(stream)  # the echoed prompt
            await anext(stream)  # the first item
            await stream.aclose()

    asyncio.run(leave_early())

    assert stored_prompts(db) == ["My topic"]


@pytest.mark.parametrize("cut", ["after the first item", "mid-reply"])
def test_the_next_turn_runs_after_an_interrupted_one(cut):
    """pydantic-ai closes out what an interrupted turn left open."""
    db = FakeDB()

    async def interrupted(messages, info: AgentInfo):
        if cut == "mid-reply" or tool_returned(messages):
            yield "Partial "
        else:
            yield {
                0: DeltaToolCall(
                    "create_new_question", '{"instructions": "q"}', tool_call_id="c1"
                )
            }
            yield {
                1: DeltaToolCall(
                    "create_new_section", '{"instructions": "s"}', tool_call_id="c2"
                )
            }
            return
        raise RuntimeError("provider went away")

    with pytest.raises(RuntimeError):
        run_chat(db, prompt="First", stream_function=interrupted)

    frames = run_chat(db, prompt="Second")

    assert frames[-1]["content"] == "Here you go."
    assert stored_prompts(db) == ["First", "Second"]
