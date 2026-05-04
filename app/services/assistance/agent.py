import json
from datetime import datetime, timezone

from pydantic import UUID4, Field
from pydantic_ai import (
    Agent,
    AgentRunError,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RunContext,
    TextPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai._agent_graph import CallToolsNode, ModelRequestNode
from pydantic_ai.messages import FunctionToolResultEvent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_graph import End

from ainterviewer.interview_guides import InterviewGuide, Question
from ainterviewer.interview_guides.generate import generate_question, generate_section
from ainterviewer.interview_guides.interview_guide import QuestionSection
from ainterviewer.settings import settings as lib_settings

from ...dependencies import DBSession
from ...services.assistance.models import AssistanceDependencies, ChatMessage

_GREETING_TRIGGER = "[system:new_session_greeting]"

DEFAULT_MODEL = OpenAIChatModel(
    "gpt-5.4-mini",
    provider=OpenAIProvider(
        api_key=lib_settings.secrets.openai_api_key.get_secret_value()  # ty:ignore[unresolved-attribute]
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
    yield (
        ChatMessage(
            role="user",
            timestamp=datetime.now(tz=timezone.utc).isoformat(),
            content=prompt,
        )
        .model_dump_json()
        .encode("utf-8")
        + b"\n"
    )

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
        while not isinstance(node, End):
            if isinstance(node, ModelRequestNode):
                async with node.stream(agent_run.ctx) as agent_stream:
                    async for text in agent_stream.stream_text(debounce_by=0.01):
                        m = ModelResponse(
                            parts=[TextPart(text)], timestamp=agent_stream.timestamp()
                        )
                        if message := to_chat_message(m):
                            yield message + b"\n"
            elif isinstance(node, CallToolsNode):
                async with node.stream(agent_run.ctx) as events:
                    async for event in events:
                        if isinstance(event, FunctionToolResultEvent) and isinstance(
                            event.result, ToolReturnPart
                        ):
                            content = event.result.content
                            timestamp = event.result.timestamp.isoformat()
                            if event.result.tool_name == "create_new_section":
                                assert isinstance(content, QuestionSection)
                                yield (
                                    ChatMessage(
                                        role="model",
                                        timestamp=timestamp,
                                        content=content.model_dump_json(),
                                        type="section",
                                    )
                                    .model_dump_json()
                                    .encode("utf-8")
                                    + b"\n"
                                )
                            elif event.result.tool_name == "create_new_question":
                                assert isinstance(content, Question)
                                yield (
                                    ChatMessage(
                                        role="model",
                                        timestamp=timestamp,
                                        content=content.model_dump_json(),
                                        type="question",
                                    )
                                    .model_dump_json()
                                    .encode("utf-8")
                                    + b"\n"
                                )
            node = await agent_run.next(node)

    if agent_run.result is None:
        raise AgentRunError("Unexpected empty result from assistance agent")

    db.assistance.add_messages(
        agent_run.result.new_messages_json(),
        session_id=session_id,
        project_id=project_id,
        user_id=user_id,
    )


def to_chat_message(m: ModelMessage) -> bytes | None:
    for part in m.parts:
        if isinstance(m, ModelRequest) and isinstance(part, UserPromptPart):
            assert isinstance(part.content, str)
            if part.content == _GREETING_TRIGGER:
                return None
            return (
                ChatMessage(
                    role="user",
                    timestamp=part.timestamp.isoformat(),
                    content=part.content,
                )
                .model_dump_json()
                .encode("utf-8")
            )

        elif isinstance(m, ModelResponse) and isinstance(part, TextPart):
            return (
                ChatMessage(
                    role="model",
                    timestamp=m.timestamp.isoformat(),
                    content=part.content,
                )
                .model_dump_json()
                .encode("utf-8")
            )
        elif isinstance(m, ModelRequest) and isinstance(part, ToolReturnPart):
            if part.tool_name == "create_new_section":
                return (
                    ChatMessage(
                        role="model",
                        timestamp=m.timestamp.isoformat(),  # ty:ignore[unresolved-attribute]
                        content=json.dumps(part.content),
                        type="section",
                    )
                    .model_dump_json()
                    .encode("utf-8")
                )
            elif part.tool_name == "create_new_question":
                return (
                    ChatMessage(
                        role="model",
                        timestamp=m.timestamp.isoformat(),  # ty:ignore[unresolved-attribute]
                        content=json.dumps(part.content),
                        type="question",
                    )
                    .model_dump_json()
                    .encode("utf-8")
                )


if __name__ == "__main__":
    print("openai:" + DEFAULT_MODEL.model_name)
