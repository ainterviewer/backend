from typing import Annotated

from fastapi import APIRouter, Form
from fastapi.responses import StreamingResponse
from pydantic import UUID4, BaseModel, Field
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIChatModel

from ainterviewer.interview_guides import InterviewGuide
from ainterviewer.interview_guides.generate import generate_question, generate_section
from ainterviewer.types import LanguageCode

from ...auth import AssistanceSessionToken
from ...dependencies import AssistanceSessionCookie, DBSession, ProjectEditor

router = APIRouter(prefix="/ws", tags=["assistance"])


class AssistanceDependencies(BaseModel):
    guide: InterviewGuide


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


async def stream_chat(
    prompt: str,
    session_id: UUID4,
    project_id: UUID4,
    user_id: UUID4,
    db: DBSession,
    guide: InterviewGuide,
):
    messages = db.assistance.get_messages(
        session_id=session_id,
        project_id=project_id,
        user_id=user_id,
    )

    async with assistance_agent.run_stream(
        prompt, message_history=messages, deps=AssistanceDependencies(guide=guide)
    ) as result:
        async for text in result.stream_output(debounce_by=0.01):
            yield text

    db.assistance.add_messages(
        result.new_messages_json(),
        session_id=session_id,
        project_id=project_id,
        user_id=user_id,
    )


@router.post("/assistance/{project_id}/{lang}/chat/")
async def send_chat(
    project_id: UUID4,
    lang: LanguageCode,
    prompt: Annotated[str, Form()],
    assistance_session: AssistanceSessionCookie | None,
    db: DBSession,
    jwt: ProjectEditor,
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
        stream_chat(prompt, session_id, project_id, jwt.user_id, db, guide),
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
    # assistance_agent.run_sync("It should be about bird watching", deps="Yashar")
    #
