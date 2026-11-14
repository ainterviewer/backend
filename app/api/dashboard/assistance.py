from fastapi import APIRouter
from fastapi.responses import Response, StreamingResponse
from pydantic import UUID4, TypeAdapter
from pydantic_ai.messages import ModelMessagesTypeAdapter

from ainterviewer.types import LanguageCode

from ...auth import AssistanceSessionToken
from ...db.crud import InterviewDataBase
from ...dependencies import AssistanceSessionCookie, DBSession, ProjectEditor
from ...schemas.content import AssistanceChatRequest
from ...services.assistance.agent import greeting, stream_messages, to_chat_messages
from ...services.assistance.models import ChatMessage

router = APIRouter(tags=["assistance"])

# Newline-delimited ChatMessages, read by the dashboard's AssistanceChat.
NDJSON = "application/x-ndjson"
SESSION_COOKIE = "assistance_session"


def _owned_session(
    cookie: AssistanceSessionToken | None,
    project_id: UUID4,
    user_id: UUID4,
    db: InterviewDataBase,
) -> UUID4 | None:
    """The cookie's session, if it is this user's in this project.

    The cookie is plain JSON the client can rewrite, so its session id is
    checked against the owner rather than trusted.
    """
    if cookie is None or cookie.project_id != project_id:
        return None
    return db.assistance.owned_session(cookie.session_id, project_id, user_id)


def _set_session_cookie(
    response: Response, project_id: UUID4, session_id: UUID4
) -> None:
    token = AssistanceSessionToken(project_id=project_id, session_id=session_id)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token.model_dump_json(),
        secure=True,
        httponly=True,
    )


@router.post("/assistance/{project_id}/{lang}/chat/new")
async def reset_session(
    project_id: UUID4, lang: LanguageCode, jwt: ProjectEditor
) -> Response:
    response = Response()
    response.delete_cookie(key=SESSION_COOKIE, secure=True, httponly=True)
    return response


@router.get("/assistance/{project_id}/{lang}/chat")
async def get_chat(
    project_id: UUID4,
    lang: LanguageCode,
    db: DBSession,
    jwt: ProjectEditor,
    assistance_session: AssistanceSessionCookie = None,
) -> Response:
    session_id = _owned_session(assistance_session, project_id, jwt.user_id, db)
    if session_id is not None:
        messages = db.assistance.get_messages(
            session_id=session_id, project_id=project_id, user_id=jwt.user_id
        )
        frames = [frame for m in messages for frame in to_chat_messages(m)]
        return Response(b"".join(frames), media_type=NDJSON)

    user = db.users.get_user_by_id(jwt.user_id)
    session_id = db.assistance.create_new_session(project_id, jwt.user_id)
    messages, frame = greeting(user.first_name)
    db.assistance.add_messages(
        ModelMessagesTypeAdapter.dump_json(messages), session_id=session_id
    )
    response = Response(frame, media_type=NDJSON)
    _set_session_cookie(response, project_id, session_id)
    return response


@router.post(
    "/assistance/{project_id}/{lang}/chat",
    responses={
        200: {"content": {NDJSON: {"schema": TypeAdapter(ChatMessage).json_schema()}}}
    },
)
async def send_chat(
    project_id: UUID4,
    lang: LanguageCode,
    request: AssistanceChatRequest,
    db: DBSession,
    jwt: ProjectEditor,
    assistance_session: AssistanceSessionCookie = None,
) -> StreamingResponse:
    session_id = _owned_session(assistance_session, project_id, jwt.user_id, db)
    new_session = session_id is None
    if session_id is None:
        session_id = db.assistance.create_new_session(project_id, jwt.user_id)

    user = db.users.get_user_by_id(jwt.user_id)
    response = StreamingResponse(
        stream_messages(
            request.prompt,
            session_id,
            project_id,
            jwt.user_id,
            db,
            request.guide,
            user_name=user.first_name,
        ),
        media_type=NDJSON,
    )
    if new_session:
        _set_session_cookie(response, project_id, session_id)
    return response
