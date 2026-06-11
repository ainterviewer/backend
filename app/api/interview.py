import shutil
from typing import Annotated

import aiohttp
from fastapi import (
    APIRouter,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi import Path as URLPath
from fastapi.responses import StreamingResponse
from jose.exceptions import JWTError
from pydantic import UUID4, ValidationError
from sqlalchemy.exc import NoResultFound
from uvicorn.config import logger

from ainterviewer.settings import settings as lib_settings
from ainterviewer.types import LanguageCode, MessageRole, TestType

from ..auth import create_interview_token, AuthToken, InterviewToken
from ..db.types import InterviewType
from ..dependencies import (
    AdminToken,
    AuthError,
    DBSession,
    GuestToken,
    ResourceRoleChecker,
    ScopeChecker,
)
from ..settings import app_settings
from ..types import CollaboratorRole, Scope, build_external_params_model
from ..utils import generate_random_filename
from .request_models import CreateInterviewRequest, SpeechRequest
from .response_models import MediaUploadResponse, MessageFeedbackResponse

router = APIRouter(tags=["interviews"])


@router.post("/projects/{project_id}/{lang}/interviews")
async def create_interview(
    request: Request,
    new_interview: CreateInterviewRequest,
    response: Response,
    db: DBSession,
    project_id: Annotated[UUID4, URLPath],
    lang: Annotated[LanguageCode, URLPath],
    user_agent: Annotated[str | None, Header()] = None,
    ip_address: Annotated[str | None, Header(alias="X-Real-IP")] = None,
) -> str:
    project = db.projects.get_project(project_id)

    try:
        project_localization = db.projects.get_project_localization(
            project_id,
            language=lang,
        )
    except NoResultFound:
        # FIXME: This should probably trigger an error allowing the user to pick
        # a language instead of just returning the default
        default_lang = project.config.default_language
        project_localization = db.projects.get_project_localization(
            project_id,
            language=default_lang,
        )

    if new_interview.interview_type == InterviewType.DISTRIBUTED:
        if project.owner.scope == Scope.DEMO:
            raise AuthError(
                status_code=403,
                detail="Forbidden, scope required: " + Scope.USER,
            )
    else:
        if not (token := request.cookies.get("access_token")):
            raise AuthError(
                status_code=403,
                detail="Forbidden, scope required: " + Scope.GUEST,
            )

        try:
            auth_token = AuthToken.decode(token)
        except (JWTError, ValidationError):
            raise AuthError(status_code=401, detail="Could not validate credentials")

        ScopeChecker(Scope.DEMO)(auth_token=auth_token)
        ResourceRoleChecker(CollaboratorRole.VIEWER, "project")(
            project_id=project_id, token=auth_token, db=db
        )

    # Validate external params against project schema
    try:
        if project.external_params and new_interview.external_params:
            params_model = build_external_params_model(project.external_params)
            params_model.model_validate(new_interview.external_params)
        elif project.external_params:
            # Check if any required params are missing
            params_model = build_external_params_model(project.external_params)
            params_model.model_validate({})
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors())

    if not (interview_guide := project_localization.interview_guide):
        raise ValueError("Interview guide is not set")

    # Apply shuffling before the interview is created, to store the shuffled
    # state
    interview_guide.shuffle()

    if new_interview.synthetic_test_type == TestType.FIXED_ANSWERS:
        interview_guide.reduce()

    participant_id = new_interview.participant_id
    if participant_id is None and new_interview.pid is not None:
        try:
            participant_id = db.participants.resolve_link_by_pid(
                project_id, new_interview.pid
            )
        except NoResultFound:
            raise HTTPException(
                status_code=404,
                detail=f"No participant with pid '{new_interview.pid}' in this project",
            )

    interview = db.interviews.create_interview(
        project_id,
        interview_guide=interview_guide,
        interview_type=new_interview.interview_type,
        interviewer=new_interview.interviewer,
        test_run_id=new_interview.test_run_id,
        user_agent=user_agent,
        referer=new_interview.referer,
        external_params=new_interview.external_params,
        ip_address=ip_address,
        language=project_localization.language,
        participant_id=participant_id,
    )

    interview_token = create_interview_token(
        project_id=project_id,
        interviewer=new_interview.interviewer,
        interview_id=interview.id,
    )

    # NOTE:
    # if we need to support iframe set samesite='none' and reconsider frontend
    # localstorage
    response.set_cookie(
        key="interview_token",
        value=interview_token,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )

    return interview_token


@router.patch("/feedback", response_model=MessageFeedbackResponse)
async def put_feedback(
    auth_token: GuestToken,
    message: MessageFeedbackResponse,
    db: DBSession,
):
    db.interviews.update_feedback(
        message_id=message.message_id,
        interview_id=message.interview_id,
        feedback=message.feedback,
    )
    return message


@router.post("/image")
async def upload_interview_image(
    auth_token: AdminToken,
    project_id: Annotated[UUID4, Form()],
    interview_id: Annotated[UUID4, Form()],
    file: Annotated[UploadFile, File()],
) -> MediaUploadResponse:
    # FIXME: This should be accessible to the users, however need better
    # security, to avoid misuse.

    filename = generate_random_filename()

    if file.filename:
        filename += "." + file.filename.split(".")[-1]

    filepath = (
        lib_settings.storage.interview_storage.image_path(interview_id) / filename
    )

    with open(filepath, "wb") as f:
        shutil.copyfileobj(file.file, f)

    return MediaUploadResponse(message="Image uploaded successfully", filename=filename)


@router.post("/audio")
async def upload_audio(
    auth_token: AdminToken,
    project_id: Annotated[UUID4, Form()],
    interview_id: Annotated[UUID4, Form()],
    file: Annotated[UploadFile, File()],
) -> MediaUploadResponse:
    # FIXME: This should be accessible to the users, however need better
    # security, to avoid misuse.

    filename = generate_random_filename()

    if file.filename:
        filename += "." + file.filename.split(".")[-1]

    filepath = (
        lib_settings.storage.interview_storage.audio_path(interview_id) / filename
    )

    with open(filepath, "wb") as f:
        shutil.copyfileobj(file.file, f)

    return MediaUploadResponse(message="Audio uploaded successfully", filename=filename)


DEFAULT_TTS_ENDPOINT = "https://api.openai.com"

# gpt-4o-mini-tts steers delivery via instructions; `speed` is only honored by
# the older tts-1 models, so pacing is stated in both places.
TTS_SPEED = 1.2
TTS_INSTRUCTIONS = (
    "You are the voice of a friendly, professional researcher reading "
    "interview questions aloud to a participant. Tone: warm, engaged and "
    "natural, never robotic or theatrical. Pacing: slightly faster than "
    "normal conversation while staying clear and easy to follow; pause "
    "briefly after a question. Language: the text is in the language with "
    "ISO 639-1 code '{language}'; pronounce it like a native speaker."
)


@router.post(
    "/speech",
    response_class=StreamingResponse,
    responses={200: {"content": {"audio/mpeg": {}}}},
)
async def synthesize_speech(
    request: Request, speech_request: SpeechRequest, db: DBSession
):
    """Synthesize speech for an interviewer message via the OpenAI-compatible
    TTS service, streaming the MP3 back as it is generated.

    Authenticated with the participant's interview token cookie, like the
    transcription endpoint. The text is looked up by message id within the
    token's interview, so participants can't synthesize arbitrary text.
    """
    token = request.cookies.get("interview_token")
    if token is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        interview_token = InterviewToken.decode(token)
    except (JWTError, ValidationError):
        raise HTTPException(status_code=401, detail="Could not validate credentials")

    speech_settings = app_settings.services.speech
    if speech_settings.tts_model is None:
        raise HTTPException(status_code=503, detail="Text-to-speech not configured")

    try:
        message = db.interviews.get_message(
            message_id=speech_request.message_id,
            interview_id=interview_token.interview_id,
            project_id=interview_token.project_id,
        )
        interview = db.interviews.get_interview(
            project_id=interview_token.project_id,
            interview_id=interview_token.interview_id,
        )
    except NoResultFound:
        raise HTTPException(status_code=404, detail="Message not found")

    if message.role == MessageRole.USER:
        raise HTTPException(
            status_code=403, detail="Only interviewer messages can be synthesized"
        )
    if not message.content.strip():
        raise HTTPException(status_code=422, detail="Message has no text content")

    # OpenAI's /v1/audio/speech caps input at 4096 characters.
    text = message.content.strip()[:4096]

    endpoint = (speech_settings.tts_endpoint or DEFAULT_TTS_ENDPOINT).rstrip("/")

    session = aiohttp.ClientSession()
    try:
        upstream = await session.post(
            f"{endpoint}/v1/audio/speech",
            headers={
                "Authorization": "Bearer "
                + lib_settings.secrets.openai_api_key.get_secret_value(),
            },
            json={
                "model": speech_settings.tts_model,
                "voice": speech_settings.tts_voice,
                "input": text,
                "response_format": "mp3",
                "speed": TTS_SPEED,
                "instructions": TTS_INSTRUCTIONS.format(language=interview.language),
            },
        )
        if upstream.status != 200:
            detail = await upstream.text()
            logger.error(f"TTS upstream error {upstream.status}: {detail}")
            raise HTTPException(status_code=502, detail="Speech synthesis failed")
    except aiohttp.ClientError as e:
        await session.close()
        logger.error(f"TTS upstream unavailable: {e!r}")
        raise HTTPException(status_code=502, detail="Speech synthesis failed")
    except HTTPException:
        await session.close()
        raise

    async def stream():
        try:
            async for chunk in upstream.content.iter_chunked(8192):
                yield chunk
        finally:
            upstream.release()
            await session.close()

    return StreamingResponse(stream(), media_type="audio/mpeg")
