import json
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Form
from fastapi.responses import Response, StreamingResponse
from pydantic import UUID4, BaseModel, Field, TypeAdapter
from pydantic_ai import (
    Agent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RunContext,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models.openai import OpenAIChatModel
from typing_extensions import TypedDict

from ainterviewer.interview_guides import InterviewGuide
from ainterviewer.interview_guides.generate import generate_question, generate_section
from ainterviewer.types import LanguageCode

from ...auth import AssistanceSessionToken
from ...dependencies import AssistanceSessionCookie, DBSession, ProjectEditor

router = APIRouter(tags=["assistance"])


class AssistanceDependencies(BaseModel):
    guide: InterviewGuide


class ChatMessage(TypedDict):
    """Format of messages sent to the browser."""

    role: Literal["user", "model"]
    timestamp: str
    content: str


system_prompt = """\
You are the AInterviewer assistant who are to assist users with constructing their interview guides.
Gain an understanding of the users interest and scope before giving suggestions or solutions.
"""

model = OpenAIChatModel("gpt-5-mini")

assistance_agent = Agent(
    model,
    system_prompt=system_prompt,
    deps_type=AssistanceDependencies,
)


@assistance_agent.tool()
async def create_new_section(
    ctx: RunContext[AssistanceDependencies],
    prompt: str = Field(description="The prompt used to generate the question."),
) -> str:
    return str(await generate_section(prompt, ctx.deps.guide))


@assistance_agent.tool()
async def create_new_question(
    ctx: RunContext[AssistanceDependencies],
    prompt: str = Field(description="The prompt used to generate the question."),
    section_idx: int = Field(
        description="The index of the section the generated question belongs to."
    ),
) -> str:
    return str(await generate_question(prompt, ctx.deps.guide, section_idx))


def to_chat_message(m: ModelMessage) -> ChatMessage | None:
    first_part = m.parts[0]
    if isinstance(m, ModelRequest) and isinstance(first_part, UserPromptPart):
        assert isinstance(first_part.content, str)
        return ChatMessage(
            role="user",
            timestamp=first_part.timestamp.isoformat(),
            content=first_part.content,
        )
    elif isinstance(m, ModelResponse) and isinstance(first_part, TextPart):
        return ChatMessage(
            role="model",
            timestamp=m.timestamp.isoformat(),
            content=first_part.content,
        )
    return None


async def stream_messages(
    prompt: str,
    session_id: UUID4,
    project_id: UUID4,
    user_id: UUID4,
    db: DBSession,
    guide: InterviewGuide,
):
    """Streams newline-delimited JSON ChatMessages to the client."""
    yield (
        json.dumps(
            ChatMessage(
                role="user",
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                content=prompt,
            )
        ).encode("utf-8")
        + b"\n"
    )

    messages = db.assistance.get_messages(
        session_id=session_id,
        project_id=project_id,
        user_id=user_id,
    )

    async with assistance_agent.run_stream(
        prompt, message_history=messages, deps=AssistanceDependencies(guide=guide)
    ) as result:
        async for text in result.stream_output(debounce_by=0.01):
            m = ModelResponse(parts=[TextPart(text)], timestamp=result.timestamp())
            yield json.dumps(to_chat_message(m)).encode("utf-8") + b"\n"

    db.assistance.add_messages(
        result.new_messages_json(),
        session_id=session_id,
        project_id=project_id,
        user_id=user_id,
    )


@router.get("/assistance/{project_id}/{lang}/chat/")
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

    chat_messages = [to_chat_message(m) for m in messages]

    return Response(
        b"\n".join(
            json.dumps(m).encode("utf-8") for m in chat_messages if m is not None
        ),
        media_type="text/plain",
    )


@router.post(
    "/assistance/{project_id}/{lang}/chat/",
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

    response = StreamingResponse(
        stream_messages(prompt, session_id, project_id, jwt.user_id, db, guide),
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
