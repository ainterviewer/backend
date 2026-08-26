from typing import Annotated, Any

import aiofiles
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

from ..auth import AuthToken, InterviewToken, create_interview_token, hash_token
from ..db.repositories.errors import ResumeTokenError
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
from ..types import (
    CollaboratorRole,
    ExternalParam,
    Scope,
    build_external_params_model,
)
from ..utils import generate_random_filename
from .request_models import (
    CreateInterviewRequest,
    SpeechRequest,
    ValidateExternalParamsRequest,
)
from .response_models import (
    InterviewResumeRedeemed,
    MediaUploadResponse,
    MessageFeedbackResponse,
)

CHUNK_SIZE = 1024 * 1024

router = APIRouter(tags=["interviews"])


def validate_external_params(
    schema: list[ExternalParam] | None,
    values: dict[str, Any] | None,
    project_id: UUID4,
) -> None:
    """Validate submitted query params against a project's declared schema.

    A project without a schema accepts anything; a project with one is
    validated against `{}` when the respondent supplied nothing, so missing
    required params are caught.

    The 422 body is deliberately opaque. Pydantic's error list names the
    parameters the link is expected to carry (and, for enums, their allowed
    values), which is exactly the hint a respondent would need to fabricate
    one. The detail is logged for the project owner instead; the interview page
    only tells the respondent the link is invalid.
    """
    if not schema:
        return

    params_model = build_external_params_model(schema)
    try:
        params_model.model_validate(values or {})
    except ValidationError as e:
        logger.info(
            f"External param validation failed for project {project_id}: {e.errors()}"
        )
        raise HTTPException(status_code=422, detail="Invalid interview link parameters")


# NOTE: Unauthenticated like the consent/welcome endpoints: the interview page
# calls this before showing anything, so a broken link fails on arrival rather
# than after the respondent has read and accepted the consent text.
@router.post("/projects/{project_id}/external_params/validate")
async def validate_interview_params(
    db: DBSession,
    project_id: Annotated[UUID4, URLPath],
    params: ValidateExternalParamsRequest,
) -> None:
    """Check a link's query params against the project's schema without
    creating an interview.

    `create_interview` runs the same check; this only moves the feedback
    earlier, it does not replace it.
    """
    project = db.projects.get_project(project_id)
    validate_external_params(
        project.external_params, params.external_params, project_id
    )


def set_interview_cookie(response: Response, interview_token: str) -> None:
    """Attach the credential the websocket handshake authenticates with.

    NOTE:
    if we need to support iframe set samesite='none' and reconsider frontend
    localstorage

    max_age is required: without it this is a session cookie that the browser
    drops on close, while the JWT inside stays valid for
    jwt_interview_token_expiration. The frontend decides whether to resume from
    a localStorage entry sized to that same expiration, so a shorter cookie
    lifetime left respondents resuming with no credential -- an unauthenticated
    websocket handshake and no way to recover. Derive it from the setting
    rather than restating the duration; that drift was the bug.

    Shared by interview creation and resume-link redemption so the two cannot
    drift apart in exactly that way again.
    """
    response.set_cookie(
        key="interview_token",
        value=interview_token,
        max_age=int(
            app_settings.app.jwt_interview_token_expiration.to_timedelta().total_seconds()
        ),
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )


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
        project_localization = db.projects.get_project_localization(
            project_id,
            language=db.projects.get_default_language(project_id),
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

    # Test runs are started from the dashboard by a collaborator, not from a
    # distributed link, so there are no link params to satisfy.
    if new_interview.interview_type == InterviewType.DISTRIBUTED:
        validate_external_params(
            project.external_params, new_interview.external_params, project_id
        )

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

    set_interview_cookie(response, interview_token)

    return interview_token


# NOTE: POST, not GET, and deliberately so. The resume link is single-use, and
# corporate mail security (Outlook Safe Links, Proofpoint and friends)
# pre-fetches URLs in email before the recipient ever clicks. A link that
# burned on GET would routinely be spent by a scanner, stranding the very
# respondent it was issued for. Scanners follow GETs; they do not submit
# forms, so the frontend lands on an interstitial and only the button redeems.
@router.post("/interviews/resume/{resume_token}")
async def redeem_interview_resume_link(
    resume_token: str,
    response: Response,
    db: DBSession,
) -> InterviewResumeRedeemed:
    """Spend a one-time resume link and hand back an interview session.

    Unauthenticated by design: the token *is* the credential, which is why it
    is high-entropy, single-use, expiring and revocable.

    Every failure is the same opaque 404. Saying which of "unknown", "already
    used" or "expired" applied would confirm to someone probing tokens that a
    guess had once been real.
    """
    try:
        interview = db.interviews.redeem_resume_token(hash_token(resume_token))
    except ResumeTokenError as exc:
        logger.info("Rejecting interview resume link: %s", exc.reason)
        raise HTTPException(
            status_code=404,
            detail="This link is no longer valid. Please ask for a new one.",
        )

    interview_token = create_interview_token(
        project_id=interview.project_id,
        interviewer=interview.interviewer,
        interview_id=interview.id,
    )
    set_interview_cookie(response, interview_token)

    logger.info(
        "interview resume link redeemed: interview=%s project=%s",
        interview.id,
        interview.project_id,
    )

    return InterviewResumeRedeemed(
        project_id=interview.project_id,
        interview_id=interview.id,
    )


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

    async with aiofiles.open(filepath, "wb") as f:
        while chunk := await file.read(CHUNK_SIZE):
            await f.write(chunk)

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

    async with aiofiles.open(filepath, "wb") as f:
        while chunk := await file.read(CHUNK_SIZE):
            await f.write(chunk)

    return MediaUploadResponse(message="Audio uploaded successfully", filename=filename)


DEFAULT_TTS_ENDPOINT = "https://api.openai.com"

# gpt-4o-mini-tts steers delivery via instructions; `speed` is only honored by
# the older tts-1 models, so pacing is stated in both places.
TTS_SPEED = 1.15
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
