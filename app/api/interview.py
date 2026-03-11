import shutil
from typing import Annotated

from fastapi import APIRouter, File, Form, Header, Request, Response, UploadFile
from fastapi import Path as URLPath
from pydantic import UUID4

from ainterviewer.settings import settings as lib_settings
from ainterviewer.types import LanguageCode, TestType

from ..auth import create_interview_token, decode_auth_token
from ..db.types import InterviewType
from ..dependencies import (
    AdminToken,
    AuthError,
    DBSession,
    GuestToken,
    ResourceRoleChecker,
    ScopeChecker,
)
from ..types import CollaboratorRole, Scope
from ..utils import generate_random_filename
from .request_models import CreateInterviewRequest
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
    except:
        # TODO: This should probably trigger an error allowing the user to pick
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
        if not (token := request.cookies.get("token")):
            raise AuthError(
                status_code=403,
                detail="Forbidden, scope required: " + Scope.GUEST,
            )

        auth_token = decode_auth_token(token)
        ScopeChecker(Scope.DEMO)(auth_token=auth_token)
        ResourceRoleChecker(CollaboratorRole.VIEWER, "project")(
            project_id=project_id, token=auth_token, db=db
        )

    if not (interview_guide := project_localization.interview_guide):
        raise ValueError("Interview guide is not set")

    # Apply shuffling before the interview is created, to store the shuffled
    # state
    interview_guide.shuffle()

    if new_interview.synthetic_test_type == TestType.FIXED_ANSWERS:
        interview_guide.reduce()

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
    )

    interview_token = create_interview_token(
        project_id=project_id,
        interviewer=new_interview.interviewer,
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
