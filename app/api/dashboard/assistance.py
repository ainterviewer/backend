from fastapi import APIRouter
from fastapi.responses import Response, StreamingResponse
from pydantic import UUID4, TypeAdapter
from pydantic_ai.messages import ModelMessagesTypeAdapter

from ainterviewer.types import LanguageCode

from ...db.crud import InterviewDataBase
from ...dependencies import DBSession, ProjectEditor
from ...schemas.content import AssistanceChatRequest
from ...services.assistance.agent import greeting, stream_messages, to_chat_messages
from ...services.assistance.models import ChatMessage

router = APIRouter(tags=["assistance"])

# Newline-delimited ChatMessages, read by the dashboard's AssistanceChat.
NDJSON = "application/x-ndjson"

# Sessions used to be picked by this cookie, one for every project, so
# opening the assistant in another project dropped the conversation in the
# first. The server now picks the session; the cookie is only cleared.
_LEGACY_SESSION_COOKIE = "assistance_session"


def _start_session(db: InterviewDataBase, project_id: UUID4, user_id: UUID4) -> bytes:
    """A new session that opens with the greeting, and the greeting's frame."""
    user = db.users.get_user_by_id(user_id)
    session_id = db.assistance.create_new_session(project_id, user_id)
    messages, frame = greeting(user.first_name)
    db.assistance.add_messages(
        ModelMessagesTypeAdapter.dump_json(messages), session_id=session_id
    )
    return frame


@router.post("/assistance/{project_id}/{lang}/chat/new")
async def reset_session(
    project_id: UUID4, lang: LanguageCode, db: DBSession, jwt: ProjectEditor
) -> Response:
    """Start a new conversation; the next read of the chat shows it."""
    _start_session(db, project_id, jwt.user_id)
    response = Response()
    response.delete_cookie(key=_LEGACY_SESSION_COOKIE, secure=True, httponly=True)
    return response


@router.get("/assistance/{project_id}/{lang}/chat")
async def get_chat(
    project_id: UUID4, lang: LanguageCode, db: DBSession, jwt: ProjectEditor
) -> Response:
    """The user's current conversation in this project, started if they have
    none yet."""
    session_id = db.assistance.current_session(project_id, jwt.user_id)
    if session_id is None:
        return Response(_start_session(db, project_id, jwt.user_id), media_type=NDJSON)

    messages = db.assistance.get_messages(session_id)
    frames = [frame for m in messages for frame in to_chat_messages(m)]
    return Response(b"".join(frames), media_type=NDJSON)


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
) -> StreamingResponse:
    # A session spans languages: switching the editor's language keeps the
    # conversation, and each turn is told which language it is in.
    session_id = db.assistance.current_session(project_id, jwt.user_id)
    if session_id is None:
        session_id = db.assistance.create_new_session(project_id, jwt.user_id)

    user = db.users.get_user_by_id(jwt.user_id)
    return StreamingResponse(
        stream_messages(
            request.prompt,
            session_id,
            db,
            request.guide,
            user_name=user.first_name,
            language=lang,
        ),
        media_type=NDJSON,
    )
