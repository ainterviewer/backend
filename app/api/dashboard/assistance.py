from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Form
from fastapi.responses import Response, StreamingResponse
from pydantic import (
    UUID4,
    TypeAdapter,
)
from pydantic_ai import (
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

from ainterviewer.interview_guides import InterviewGuide
from ainterviewer.types import LanguageCode

from ...auth import AssistanceSessionToken
from ...dependencies import AssistanceSessionCookie, DBSession, ProjectEditor
from ...services.assistance.agent import (
    _GREETING_TRIGGER,
    GREETING_TEMPLATE,
    stream_messages,
    to_chat_message,
)
from ...services.assistance.models import (
    ChatMessage,
    _messages_adapter,
)

router = APIRouter(tags=["assistance"])


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
        user = db.users.get_user_by_id(jwt.user_id)
        session_id = db.assistance.create_new_session(project_id, jwt.user_id)

        greeting_text = GREETING_TEMPLATE.format(user_name=user.first_name)
        greeting_timestamp = datetime.now(tz=timezone.utc)
        db.assistance.add_messages(
            _messages_adapter.dump_json(
                [
                    ModelRequest(
                        parts=[
                            UserPromptPart(
                                content=_GREETING_TRIGGER, timestamp=greeting_timestamp
                            )
                        ]
                    ),
                    ModelResponse(
                        parts=[TextPart(greeting_text)], timestamp=greeting_timestamp
                    ),
                ]
            ),
            session_id=session_id,
            project_id=project_id,
            user_id=jwt.user_id,
        )

        response = Response(
            ChatMessage(
                role="model",
                timestamp=greeting_timestamp.isoformat(),
                content=greeting_text,
            )
            .model_dump_json()
            .encode("utf-8"),
            media_type="text/plain",
        )
        response.set_cookie(
            key="assistance_session",
            value=AssistanceSessionToken(
                project_id=project_id, session_id=session_id
            ).model_dump_json(),
            secure=True,
            httponly=True,
        )
        return response

    messages = db.assistance.get_messages(
        session_id=assistance_session.session_id,
        project_id=project_id,
        user_id=jwt.user_id,
    )

    chat_messages = [message for m in messages if (message := to_chat_message(m))]

    return Response(
        b"\n".join(chat_messages),
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
