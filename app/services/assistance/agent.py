from datetime import UTC, datetime
from typing import Literal

from pydantic import UUID4, Field
from pydantic_ai import (
    Agent,
    AgentRunError,
    ModelMessage,
    ModelResponse,
    RunContext,
    TextPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.messages import FunctionToolResultEvent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from ainterviewer.interview_guides import InterviewGuide, Question, QuestionSection
from ainterviewer.interview_guides.generate import generate_question, generate_section
from ainterviewer.settings import settings as lib_settings

from ...dependencies import DBSession
from ...services.assistance.models import AssistanceDependencies, ChatMessage

_GREETING_TRIGGER = "[system:new_session_greeting]"

DEFAULT_MODEL = OpenAIChatModel(
    "gpt-5.4-mini",
    provider=OpenAIProvider(
        api_key=lib_settings.secrets.openai_api_key.get_secret_value()
    ),
)


def agent_instruction(ctx: RunContext[AssistanceDependencies]) -> str:
    return f"""\
You are a helpful assistant whos job it is to assist users with constructing
their interview guides for qualitative research purposes.

The interview guide and interviews are constructed and conducted on the
platform on which you run: AInterviewer.

You should gain an understanding of the users interest and scope of the
interview before giving suggestions or solutions.

Keep your messages and questions brief, so that it's easy for the user to
provide feedback.

Refer to the user's name {ctx.deps.user_name} when appropiate.

Do not reiterate the output of tool calls, the user will see them in a
different interface.

You are not able to edit the interview guide directly, only provide feedback on
it, or generate suggestions for sections and questions that the user can insert
themself.

This is the state of the users interview guide:
```
{ctx.deps.guide.model_dump_json()}
```
"""


GREETING_TEMPLATE = (
    "Hi {user_name}! I'm here to help you build your interview guide. "
    "Feel free to share your research topic and I'll help you craft great questions."
)

assistance_agent = Agent(
    DEFAULT_MODEL,
    instructions=agent_instruction,
    deps_type=AssistanceDependencies,
)


@assistance_agent.tool()
async def create_new_section(
    ctx: RunContext[AssistanceDependencies],
    instructions: str = Field(
        description="The instructions used to generate the question."
    ),
) -> QuestionSection:
    """Generates an new section with grouped interview questions based on the
    existing interview guide and the users prompt.

    You should not reiterate the output, it will be shown to the user in another interface.
    """
    return await generate_section(
        instructions,
        "openai:" + DEFAULT_MODEL.model_name,
        ctx.deps.guide,
    )


@assistance_agent.tool()
async def create_new_question(
    ctx: RunContext[AssistanceDependencies],
    instructions: str = Field(
        description="The instructions used to generate the question."
    ),
    section: QuestionSection | None = Field(
        None, description="The existing section the generated question belongs to."
    ),
) -> Question:
    """Generates an new interview questions based on the existing interview
    guide, the users prompt, and the index of the section to which the question
    fits.

    You should not reiterate the output, it will be shown to the user in another interface.
    """
    return await generate_question(
        instructions,
        "openai:" + DEFAULT_MODEL.model_name,
        ctx.deps.guide,
        section=section,
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
    return (
        ChatMessage(
            role=role, timestamp=timestamp.isoformat(), content=content, type=type
        )
        .model_dump_json()
        .encode("utf-8")
    )


def _tool_frame(part: ToolReturnPart) -> bytes | None:
    if (frame_type := _TOOL_FRAME_TYPES.get(part.tool_name)) is None:
        return None
    # Content is the tool's model on a live run and plain JSON data once read
    # back from the database; model_response_str serialises either.
    return _frame("model", part.timestamp, part.model_response_str(), frame_type)


async def stream_messages(
    prompt: str,
    session_id: UUID4,
    project_id: UUID4,
    user_id: UUID4,
    db: DBSession,
    guide: InterviewGuide,
    user_name: str,
):
    """Streams newline-delimited JSON ChatMessages to the client."""
    yield _frame("user", datetime.now(tz=UTC), prompt) + b"\n"

    messages = db.assistance.get_messages(
        session_id=session_id,
        project_id=project_id,
        user_id=user_id,
    )

    async with assistance_agent.iter(
        prompt,
        message_history=messages,
        deps=AssistanceDependencies(guide=guide, user_name=user_name),
    ) as agent_run:
        node = agent_run.next_node
        while not Agent.is_end_node(node):
            if Agent.is_model_request_node(node):
                async with node.stream(agent_run.ctx) as agent_stream:
                    # Each chunk is the reply so far, not a delta: the client
                    # replaces the message it is showing with it.
                    async for text in agent_stream.stream_text(debounce_by=0.01):
                        yield _frame("model", agent_stream.timestamp, text) + b"\n"
            elif Agent.is_call_tools_node(node):
                async with node.stream(agent_run.ctx) as events:
                    async for event in events:
                        if (
                            isinstance(event, FunctionToolResultEvent)
                            and isinstance(event.part, ToolReturnPart)
                            and (frame := _tool_frame(event.part))
                        ):
                            yield frame + b"\n"
            node = await agent_run.next(node)

    if agent_run.result is None:
        raise AgentRunError("Unexpected empty result from assistance agent")

    db.assistance.add_messages(
        agent_run.result.new_messages_json(),
        session_id=session_id,
        project_id=project_id,
        user_id=user_id,
    )


def to_chat_messages(m: ModelMessage) -> list[bytes]:
    """The chat frames a stored message replays as: one per part shown.

    A turn can hold several of them -- the agent may call both tools at once --
    so every part is rendered, not just the first.
    """
    frames: list[bytes] = []
    for part in m.parts:
        if isinstance(part, UserPromptPart):
            assert isinstance(part.content, str)
            if part.content != _GREETING_TRIGGER:
                frames.append(_frame("user", part.timestamp, part.content))
        elif isinstance(m, ModelResponse) and isinstance(part, TextPart):
            frames.append(_frame("model", m.timestamp, part.content))
        elif isinstance(part, ToolReturnPart) and (frame := _tool_frame(part)):
            frames.append(frame)
    return frames
