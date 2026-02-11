import shutil
from typing import Annotated

from fastapi import (
    APIRouter,
    Cookie,
    File,
    Form,
    Header,
    Response,
    UploadFile,
)
from fastapi import Path as URLPath
from pydantic import UUID4

from ainterviewer.settings import settings as lib_settings
from ainterviewer.types import LanguageCode, TestType

from ..auth import create_interview_token
from ..dependencies import AdminToken, DBSession, GuestToken
from ..utils import generate_random_filename
from .request_models import CreateInterviewRequest
from .response_models import MediaUploadResponse, MessageFeedbackResponse

router = APIRouter(tags=["interviews"])


@router.post("/projects/{project_id}/{lang}/interviews")
async def create_interview(
    request: CreateInterviewRequest,
    response: Response,
    db: DBSession,
    project_id: Annotated[UUID4, URLPath],
    lang: Annotated[LanguageCode, URLPath],
    user_agent: Annotated[str | None, Header()] = None,
    ip_address: Annotated[str | None, Header(alias="X-Real-IP")] = None,
    referer: Annotated[str | None, Cookie()] = None,
    forward_params: Annotated[str | None, Cookie()] = None,
) -> str:
    try:
        project_localization = db.projects.get_project_localization(
            project_id,
            language=lang,
        )
    except:
        project = db.projects.get_project(project_id)
        default_lang = project.config.default_language
        project_localization = db.projects.get_project_localization(
            project_id,
            language=default_lang,
        )

    if not (interview_guide := project_localization.interview_guide):
        raise ValueError("Interview guide is not set")

    # Apply shuffling before the interview is created, to store the shuffled
    # state
    interview_guide.shuffle()

    if request.synthetic_test_type == TestType.FIXED_ANSWERS:
        interview_guide.reduce()

    interview = db.interviews.create_interview(
        project_id,
        interview_guide=interview_guide,
        interview_type=request.interview_type,
        interviewer=request.interviewer,
        test_run_id=request.test_run_id,
        user_agent=user_agent,
        referer=referer,
        external_params=forward_params,
        ip_address=ip_address,
    )

    interview_token = create_interview_token(
        project_id=project_id,
        interviewer=request.interviewer,
        interview_id=interview.id,
    )

    response.set_cookie("interview_token", interview_token)

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
async def upload_image(
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
