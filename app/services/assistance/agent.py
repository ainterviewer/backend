import logging
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from functools import cache
from typing import Literal
from uuid import UUID

from pydantic import UUID4
from pydantic_ai import (
    Agent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelRetry,
    RunContext,
    TextPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import (
    FunctionToolResultEvent,
    ModelMessagesTypeAdapter,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    TextPartDelta,
)
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.openrouter import OpenRouterProvider

from ainterviewer.interview_guides import InterviewGuide, Question, QuestionSection
from ainterviewer.interview_guides.generate import generate_question, generate_section
from ainterviewer.settings import settings as lib_settings

from ...db.crud import InterviewDataBase
from ...services.assistance.models import AssistanceDependencies, ChatMessage
from ...settings import app_settings

logger = logging.getLogger(__name__)

_GREETING_TRIGGER = "[system:new_session_greeting]"

# `provider:model`, as the lib's generators take it too.
ASSISTANCE_MODEL = app_settings.services.assistance.model

# How often a streaming reply is re-sent at most. Every frame carries the whole
# reply so far, so a frame per token would send it quadratically.
_TEXT_FRAME_INTERVAL = 0.05


@cache
def assistance_model() -> Model:
    """The agent's model, built on first use rather than at import.

    Building it creates the provider's client, which refuses an empty API key.
    At import that took the whole backend down with it on any deployment
    without that key; now only the assistant fails, and only when used.
    """
    provider, _, name = ASSISTANCE_MODEL.partition(":")
    secrets = lib_settings.secrets
    if provider == "openrouter":
        return OpenRouterModel(
            name,
            provider=OpenRouterProvider(
                api_key=secrets.openrouter_api_key.get_secret_value()
            ),
        )
    return OpenAIChatModel(
        name,
        provider=OpenAIProvider(api_key=secrets.openai_api_key.get_secret_value()),
    )


def agent_instruction(ctx: RunContext[AssistanceDependencies]) -> str:
    return f"""\
You are a helpful assistant whose job is to assist users with constructing
their interview guides for qualitative research purposes.

The interview guide and interviews are constructed and conducted on the
platform on which you run: AInterviewer.

You should gain an understanding of the user's interest and the scope of the
interview before giving suggestions or solutions.

Keep your messages and questions brief, so that it's easy for the user to
provide feedback.

Refer to the user's name, {ctx.deps.user_name}, when appropriate.

Do not reiterate the output of tool calls, the user will see them in a
different interface.

You are not able to edit the interview guide directly, only provide feedback on
it, or generate suggestions for sections and questions that the user can insert
themselves.

This is the state of the user's interview guide:
```
{ctx.deps.guide.model_dump_json()}
```
"""


GREETING_TEMPLATE = (
    "Hi {user_name}! I'm here to help you build your interview guide. "
    "Feel free to share your research topic and I'll help you craft great questions."
)


def greeting(user_name: str) -> tuple[list[ModelMessage], bytes]:
    """A new session's opening: the messages to store, and the frame to show.

    The greeting is stored as a reply to a placeholder prompt, which replay
    hides, so the history the model reads starts with a user turn as every
    provider accepts.
    """
    timestamp = datetime.now(tz=UTC)
    text = GREETING_TEMPLATE.format(user_name=user_name)
    messages: list[ModelMessage] = [
        ModelRequest(
            parts=[UserPromptPart(content=_GREETING_TRIGGER, timestamp=timestamp)]
        ),
        ModelResponse(parts=[TextPart(text)], timestamp=timestamp),
    ]
    return messages, _frame("model", timestamp, text)


# No model here: stream_messages passes assistance_model() per run.
assistance_agent = Agent(
    instructions=agent_instruction,
    deps_type=AssistanceDependencies,
)


@assistance_agent.tool
async def create_new_section(
    ctx: RunContext[AssistanceDependencies], instructions: str
) -> QuestionSection:
    """Generate a new section of grouped interview questions, based on the
    existing interview guide and the user's request.

    Do not reiterate the output; the user sees it in another interface.

    Args:
        instructions: What the section should cover, and anything it must take
            into account.
    """
    return await generate_section(instructions, ASSISTANCE_MODEL, ctx.deps.guide)


@assistance_agent.tool
async def create_new_question(
    ctx: RunContext[AssistanceDependencies],
    instructions: str,
    section_id: UUID | None = None,
) -> Question:
    """Generate a new interview question, based on the existing interview guide
    and the user's request.

    Do not reiterate the output; the user sees it in another interface.

    Args:
        instructions: What the question should ask about, and anything it must
            take into account.
        section_id: The `id` of the existing section in the interview guide the
            question belongs to. Omit it for a question that fits no section.
    """
    section = None
    if section_id is not None:
        sections = ctx.deps.guide.question_sections
        section = next((s for s in sections if s.id == section_id), None)
        if section is None:
            raise ModelRetry(
                f"The interview guide has no section with id {section_id}; "
                "use an `id` from its `question_sections`, or omit `section_id`."
            )
    return await generate_question(
        instructions, ASSISTANCE_MODEL, ctx.deps.guide, section=section
    )


# The tools whose results the chat shows as an insertable item, by frame type.
_TOOL_FRAME_TYPES: dict[str, Literal["question", "section"]] = {
    "create_new_question": "question",
    "create_new_section": "section",
}


def _frame(
    role: Literal["user", "model"],
    timestamp: datetime,
    content: str,
    type: Literal["message", "question", "section"] = "message",
) -> bytes:
    """One newline-terminated ChatMessage, as the chat reads it."""
    message = ChatMessage(
        role=role, timestamp=timestamp.isoformat(), content=content, type=type
    )
    return message.model_dump_json().encode("utf-8") + b"\n"


def _tool_frame(part: ToolReturnPart) -> bytes | None:
    """The insertable item a tool returned, if it returned one.

    A call that failed, was retried or was cut short also comes back as a
    return part, carrying an error for the model instead of the item.
    """
    frame_type = _TOOL_FRAME_TYPES.get(part.tool_name)
    if frame_type is None or part.outcome != "success":
        return None
    # Content is the tool's model on a live run and plain JSON data once read
    # back from the database; model_response_str serialises either.
    return _frame("model", part.timestamp, part.model_response_str(), frame_type)


class _Reply:
    """The text of the response being streamed, as the frames to send for it.

    Every frame carries the whole reply so far, so frames are throttled to one
    per `_TEXT_FRAME_INTERVAL`; the finished text always goes out.
    """

    def __init__(self) -> None:
        self.texts: dict[int, str] = {}  # by part index
        self.started = datetime.now(tz=UTC)
        self.last_sent = 0.0

    def frame(self) -> bytes:
        self.last_sent = time.monotonic()
        text = "".join(self.texts[i] for i in sorted(self.texts))
        return _frame("model", self.started, text)

    def on_event(self, event: object) -> bytes | None:
        if isinstance(event, PartStartEvent):
            if event.index == 0:  # a new response
                self.__init__()
            if isinstance(event.part, TextPart):
                self.texts[event.index] = event.part.content
        elif isinstance(event, PartDeltaEvent) and isinstance(
            event.delta, TextPartDelta
        ):
            self.texts[event.index] = (
                self.texts.get(event.index, "") + event.delta.content_delta
            )
            if time.monotonic() - self.last_sent >= _TEXT_FRAME_INTERVAL:
                return self.frame()
        elif isinstance(event, PartEndEvent) and isinstance(event.part, TextPart):
            self.texts[event.index] = event.part.content
            return self.frame()
        return None


async def stream_messages(
    prompt: str,
    session_id: UUID4,
    project_id: UUID4,
    user_id: UUID4,
    db: InterviewDataBase,
    guide: InterviewGuide,
    user_name: str,
) -> AsyncIterator[bytes]:
    """Streams newline-delimited JSON ChatMessages to the client.

    A model reply is sent as it grows, each frame carrying the whole reply so
    far: the client replaces the message it is showing with it. Tool results
    the chat can insert follow as frames of their own.

    The run's messages are stored however it ends -- completed, failed, or cut
    short by the client going away -- so the user's prompt and any generated
    items stay in the history. pydantic-ai closes out a turn that stopped
    part-way when the next run reads it back.
    """
    yield _frame("user", datetime.now(tz=UTC), prompt)

    history = db.assistance.get_messages(
        session_id=session_id, project_id=project_id, user_id=user_id
    )
    deps = AssistanceDependencies(guide=guide, user_name=user_name)

    async with assistance_agent.run_stream_events(
        prompt, model=assistance_model(), message_history=history, deps=deps
    ) as events:
        reply = _Reply()
        completed = False
        try:
            async for event in events:
                if isinstance(event, FunctionToolResultEvent):
                    if isinstance(event.part, ToolReturnPart) and (
                        frame := _tool_frame(event.part)
                    ):
                        yield frame
                elif frame := reply.on_event(event):
                    yield frame
            completed = True
        finally:
            try:
                new_messages = events.new_messages()
            except UserError:  # the run never started
                new_messages = []
            if new_messages:
                _store(db, session_id, new_messages, raise_errors=completed)


def _store(
    db: InterviewDataBase,
    session_id: UUID4,
    messages: list[ModelMessage],
    raise_errors: bool,
) -> None:
    try:
        db.assistance.add_messages(
            ModelMessagesTypeAdapter.dump_json(messages), session_id=session_id
        )
    except Exception:
        if raise_errors:
            raise
        # The run already failed; raising here would hide why.
        logger.exception("Could not store the interrupted assistance run")


def to_chat_messages(m: ModelMessage) -> list[bytes]:
    """The chat frames a stored message replays as: one per part shown.

    A turn can hold several of them -- the agent may call both tools at once --
    so every part is rendered, not just the first.
    """
    frames: list[bytes] = []
    for part in m.parts:
        if isinstance(part, UserPromptPart):
            if isinstance(part.content, str) and part.content != _GREETING_TRIGGER:
                frames.append(_frame("user", part.timestamp, part.content))
        elif isinstance(m, ModelResponse) and isinstance(part, TextPart):
            frames.append(_frame("model", m.timestamp, part.content))
        elif isinstance(part, ToolReturnPart) and (frame := _tool_frame(part)):
            frames.append(frame)
    return frames
