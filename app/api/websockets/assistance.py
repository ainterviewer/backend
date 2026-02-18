import json
import random
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import StreamingResponse
from pydantic import UUID4, BaseModel
from pydantic_ai import Agent, RunContext, Tool
from pydantic_ai.models.openai import OpenAIChatModel

from ...auth import AssistanceSessionToken
from ...dependencies import AssistanceSessionCookie, DBSession, ProjectEditor

router = APIRouter(prefix="/ws", tags=["assistance"])


class AssistanceDependencies(BaseModel): ...


system_prompt = """\
You are the AInterviewer assistant who are to assist users with constructing their interview guides.
Gain an understanding of the users interest and scope before giving suggestions or solutions.
"""


def roll_dice() -> str:
    """Roll a six-sided die and return the result."""
    return str(random.randint(1, 6))


def get_player_name(ctx: RunContext[str]) -> str:
    """Get the player's name."""
    return ctx.deps


model = OpenAIChatModel("gpt-5-mini")

assistance_agent = Agent(
    model,
    system_prompt=system_prompt,
    deps_type=str,
    tools=[Tool(roll_dice), Tool(get_player_name)],
)


@router.post("/assistance/{project_id}/chat/")
async def post_chat(
    request: Request,
    project_id: UUID4,
    prompt: Annotated[str, Form()],
    assistance_session: AssistanceSessionCookie | None,
    db: DBSession,
    jwt: ProjectEditor,
) -> StreamingResponse:
    async def stream_messages():
        """Streams new line delimited JSON `Message`s to the client."""

        messages = db.assistance.get_messages(
            session_id=session_id,
            project_id=project_id,
            user_id=jwt.user_id,
        )

        async with assistance_agent.run_stream(
            prompt, message_history=messages
        ) as result:
            async for text in result.stream_output(debounce_by=0.01):
                # text here is a `str` and the frontend wants
                # JSON encoded ModelResponse, so we create one
                m = ModelResponse(parts=[TextPart(text)], timestamp=result.timestamp())
                yield json.dumps(to_chat_message(m)).encode("utf-8") + b"\n"

        db.assistance.add_messages(
            result.new_messages_json(),
            session_id=session_id,
            project_id=project_id,
            user_id=jwt.user_id,
        )

    response = StreamingResponse(stream_messages(), media_type="text/plain")

    if not assistance_session or assistance_session.project_id != project_id:
        session_id = db.assistance.create_new_session(project_id, jwt.user_id)
        assistance_session_token = AssistanceSessionToken(
            project_id=project_id, session_id=session_id
        )
        response.set_cookie(
            key="assistance_session",
            value=assistance_session_token.model_dump_json(),
            secure=True,
            httponly=True,
        )

    else:
        session_id = assistance_session.session_id

    return response


if __name__ == "__main__":
    print(system_prompt)
    # assistance_agent.run_sync("It should be about bird watching", deps="Yashar")
    #
