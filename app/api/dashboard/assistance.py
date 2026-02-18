import json
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Form
from fastapi.responses import Response, StreamingResponse
from pydantic import (
    UUID4,
    BaseModel,
    Field,
    TypeAdapter,
    model_validator,
)
from pydantic_ai import (
    Agent,
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
from pydantic_graph import End

from ainterviewer.interview_guides import InterviewGuide, Question
from ainterviewer.interview_guides.generate import generate_question, generate_section
from ainterviewer.interview_guides.interview_guide import QuestionSection
from ainterviewer.types import LanguageCode

from ...auth import AssistanceSessionToken
from ...dependencies import AssistanceSessionCookie, DBSession, ProjectEditor

router = APIRouter(tags=["assistance"])


class AssistanceDependencies(BaseModel):
    guide: InterviewGuide
    user_name: str


class ChatMessage(BaseModel):
    """Format of messages sent to the browser."""

    type: Literal["message", "question", "section"] = "message"
    role: Literal["user", "model"]
    timestamp: str
    content: str

    @model_validator(mode="after")
    def validate_type(self):
        match self.type:
            case "question":
                Question.model_validate_json(self.content)
            case "section":
                QuestionSection.model_validate_json(self.content)

        return self


system_prompt = """\
You are a helpful assistant whos job it is to assist users with constructing their interview guides for qualitative research purposes.

The interview guide and interviews are constructed and conducted on the platform on which you run: AInterviewer.

You should gain an understanding of the users interest and scope of the interview before giving suggestions or solutions.
"""


def agent_instruction(ctx: RunContext[AssistanceDependencies]) -> str:
    return f"""\
Always use user name {ctx.deps.user_name} while responding.

Do not reiterate the output of tool calls, the user will see them in a different interface.

This is the state of the users interview guide:
```
{ctx.deps.guide.model_dump_json()}
```
"""


model = OpenAIChatModel("gpt-5-mini")

assistance_agent = Agent(
    model,
    system_prompt=system_prompt,
    instructions=agent_instruction,
    deps_type=AssistanceDependencies,
)


@assistance_agent.tool()
async def create_new_section(
    ctx: RunContext[AssistanceDependencies],
    prompt: str = Field(description="The prompt used to generate the question."),
) -> QuestionSection:
    """Generates an new section with grouped interview questions based on the
    existing interview guide and the users prompt.

    You should not reiterate the output, it will be shown to the user in another interface.
    """
    return await generate_section(prompt, ctx.deps.guide)


@assistance_agent.tool()
async def create_new_question(
    ctx: RunContext[AssistanceDependencies],
    prompt: str = Field(description="The prompt used to generate the question."),
    section: QuestionSection | None = Field(
        None, description="The existing section the generated question belongs to."
    ),
) -> Question:
    """Generates an new interview questions based on the existing interview
    guide, the users prompt, and the index of the section to which the question
    fits.

    You should not reiterate the output, it will be shown to the user in another interface.
    """
    return await generate_question(prompt, ctx.deps.guide, section=section)


def to_chat_message(m: ModelMessage) -> bytes | None:
    for part in m.parts:
        if isinstance(m, ModelRequest) and isinstance(part, UserPromptPart):
            assert isinstance(part.content, str)
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
                        timestamp=m.timestamp.isoformat(),
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
                        timestamp=m.timestamp.isoformat(),
                        content=json.dumps(part.content),
                        type="question",
                    )
                    .model_dump_json()
                    .encode("utf-8")
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

    db.assistance.add_messages(
        agent_run.result.new_messages_json(),
        session_id=session_id,
        project_id=project_id,
        user_id=user_id,
    )


@router.post("/assistance/{project_id}/{lang}/chat/new")
async def reset_session(
    project_id: UUID4,
    lang: LanguageCode,
    db: DBSession,
    jwt: ProjectEditor,
    assistance_session: AssistanceSessionCookie = None,
):
    response = Response()

    response.delete_cookie(
        key="assistance_session",
        secure=True,
        httponly=True,
    )
    return response


@router.get("/assistance/{project_id}/{lang}/chat")
async def get_chat(
    project_id: UUID4,
    lang: LanguageCode,
    db: DBSession,
    jwt: ProjectEditor,
    assistance_session: AssistanceSessionCookie = None,
) -> Response:
    if not assistance_session or assistance_session.project_id != project_id:
        return Response(b"", media_type="text/plain")

    messages = db.assistance.get_messages(
        session_id=assistance_session.session_id,
        project_id=project_id,
        user_id=jwt.user_id,
    )

    chat_messages = [message for m in messages if (message := to_chat_message(m))]

    return Response(
        b"\n".join(m for m in chat_messages if m is not None),
        media_type="text/plain",
    )


@router.post(
    "/assistance/{project_id}/{lang}/chat",
    responses={
        200: {
            "content": {
                "text/event-stream": {"schema": TypeAdapter(ChatMessage).json_schema()}
            }
        }
    },
)
async def send_chat(
    project_id: UUID4,
    lang: LanguageCode,
    prompt: Annotated[str, Form()],
    # TODO: Should Session be bound to language?
    db: DBSession,
    jwt: ProjectEditor,
    assistance_session: AssistanceSessionCookie = None,
) -> StreamingResponse:
    project_localization = db.projects.get_project_localization(project_id, lang)
    guide = project_localization.interview_guide or InterviewGuide()

    if not assistance_session or assistance_session.project_id != project_id:
        session_id = db.assistance.create_new_session(project_id, jwt.user_id)
        new_session = True
    else:
        session_id = assistance_session.session_id
        new_session = False

    user = db.users.get_user_by_id(jwt.user_id)

    response = StreamingResponse(
        stream_messages(
            prompt,
            session_id,
            project_id,
            jwt.user_id,
            db,
            guide,
            user_name=user.first_name,
        ),
        media_type="text/plain",
    )

    if new_session:
        assistance_session_token = AssistanceSessionToken(
            project_id=project_id, session_id=session_id
        )
        response.set_cookie(
            key="assistance_session",
            value=assistance_session_token.model_dump_json(),
            secure=True,
            httponly=True,
        )

    return response


if __name__ == "__main__":
    print(system_prompt)
