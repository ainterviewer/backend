import shutil
from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter,
    Cookie,
    File,
    Form,
    Header,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi import Path as URLPath
from fastapi.responses import HTMLResponse
from pydantic import UUID4

from ainterviewer.types import Interviewer, LanguageCode

from ..auth import create_interview_token, decode_interview_token
from ..dependencies import (
    AdminToken,
    DBSession,
    GuestToken,
    LanguageCookie,
    templates,
)
from ..translations import MODALS
from ..utils import generate_random_filename
from .models import MessageFeedback

router = APIRouter(tags=["interviews"])


@router.post("/projects/{project_id}/consent/render")
async def render_consent_modal(
    request: Request,
    db: DBSession,
    language: LanguageCookie,
    project_id: Annotated[UUID4, URLPath],
) -> HTMLResponse:
    project_localization = db.projects.get_project_localization(
        project_id,
        language=language,
    )

    consent = (
        MODALS["consent"].get(language, MODALS["consent"]["EN"]) | consent.model_dump()
        if (consent := project_localization.consent)
        else {}
    )

    return templates.TemplateResponse(
        "site/interview/components/ConsentModal.jinja",
        context={
            "request": request,
            "project_id": project_id,
            "consent": consent,
            "display_modal": True,
        },
    )


@router.post("/projects/{project_id}/welcome/render")
async def set_consent(
    request: Request,
    db: DBSession,
    language: LanguageCookie,
    project_id: Annotated[UUID4, URLPath],
    interview_token: Annotated[str, Cookie()],
) -> HTMLResponse:
    token = decode_interview_token(interview_token)

    project_localization = db.projects.get_project_localization(
        project_id,
        team_id=None,
        language=language,
    )

    if project_localization.interview_guide.welcome is None:
        welcome = {}
    else:
        welcome = project_localization.interview_guide.welcome.model_dump()

    welcome_modal = MODALS["welcome"].get(language, MODALS["welcome"]["EN"]).copy()

    welcome = dict(
        section_before_id=welcome_modal.pop("section_before_id").format(
            email=welcome.pop("email") if "email" in welcome else "",
        ),
        section_after_id=welcome_modal.pop("section_after_id"),
        **welcome_modal | welcome,
    )

    return templates.TemplateResponse(
        "site/interview/components/WelcomeModal.jinja",
        context={
            "request": request,
            "interview_id": token.interview_id,
            "welcome": welcome,
            "display_modal": True,
        },
    )


@router.post("/projects/{project_id}/{lang}/interviews")
async def create_interview(
    response: Response,
    db: DBSession,
    project_id: Annotated[UUID4, URLPath],
    lang: Annotated[LanguageCode, URLPath],
    interviewer: Annotated[Interviewer, Query()] = Interviewer.AI,
    synthetic: Annotated[bool, Query()] = False,
    fixed_answers: Annotated[bool, Query()] = False,
    user_agent: Annotated[str | None, Header()] = None,
    ip_address: Annotated[str | None, Header(alias="X-Real-IP")] = None,
    referer: Annotated[str | None, Cookie()] = None,
    forward_params: Annotated[str | None, Cookie()] = None,
) -> str:
    project_localization = db.projects.get_project_localization(
        project_id,
        language=lang,
    )

    if not (interview_guide := project_localization.interview_guide):
        raise ValueError("Interview guide is not set")

    # Apply shuffling before the interview is created, to store the shuffled
    # state
    interview_guide.shuffle()

    if fixed_answers:
        interview_guide.reduce()

    interview = db.interviews.create_interview(
        project_id,
        interview_guide=interview_guide,
        interviewer=interviewer,
        synthetic=synthetic,
        user_agent=user_agent,
        referer=referer,
        external_params=forward_params,
        ip_address=ip_address,
    )

    interview_token = create_interview_token(
        project_id=project_id,
        interviewer=interviewer,
        interview_id=interview.id,
    )

    response.set_cookie("interview_token", interview_token)

    return interview_token


@router.patch("/feedback", response_model=MessageFeedback)
async def put_feedback(
    auth_token: GuestToken,
    message: MessageFeedback,
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
):
    # FIXME: This should be accessible to the users, however need better
    # security, to avoid misuse.

    filename = generate_random_filename()
    if file.filename:
        filename += "." + file.filename.split(".")[-1]

    folder: Path = Path(f"data/images/{project_id}/{interview_id}")
    folder.mkdir(parents=True, exist_ok=True)

    with open(folder / filename, "wb") as f:
        shutil.copyfileobj(file.file, f)

    return {"message": "Image uploaded successfully", "filename": filename}
