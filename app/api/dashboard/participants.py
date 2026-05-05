from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Request, UploadFile
from jinja2 import TemplateError
from pydantic import UUID4

from ainterviewer.types import LanguageCode

from ...db.models import (
    ParticipantCreate,
    ParticipantPublic,
    ParticipantUpdate,
)
from ...dependencies import (
    DBSession,
    ProjectAdmin,
    ProjectEditor,
    ProjectViewer,
    UserToken,
)
from ...services.email.mail import send_email
from ...services.email.participant_template import (
    TemplateSyntaxError,
    build_template_context,
    render_participant_email_template,
    validate_participant_email_template,
)
from ..request_models import (
    DeleteParticipantsRequest,
    LinkParticipantRequest,
    ParticipantEmailTemplateRequest,
    SendParticipantEmailRequest,
)
from ..response_models import SendParticipantEmailResponse

router = APIRouter(tags=["participants"])


@router.get("/projects/{project_id}/participants")
async def get_participants(
    project_id: UUID4,
    db: DBSession,
    jwt: UserToken,
    _: ProjectViewer,
) -> list[ParticipantPublic]:
    return db.participants.get_participants(project_id)


@router.get("/projects/{project_id}/participants/{participant_id}")
async def get_participant(
    project_id: UUID4,
    participant_id: UUID4,
    db: DBSession,
    jwt: UserToken,
    _: ProjectViewer,
) -> ParticipantPublic:
    return db.participants.get_participant(participant_id)


@router.post("/participants/{participant_pid}")
async def opt_out(
    participant_pid: str,
    db: DBSession,
    reason: Annotated[str | None, Body()] = None,
) -> ParticipantPublic:
    return db.participants.opt_out(participant_pid, reason)


@router.post("/projects/{project_id}/participants")
async def add_participant(
    project_id: UUID4,
    participant: ParticipantCreate,
    db: DBSession,
    jwt: UserToken,
    _: ProjectEditor,
) -> ParticipantPublic:
    return db.participants.add_participant(project_id, participant)


@router.post("/projects/{project_id}/participants/bulk")
async def add_participants(
    project_id: UUID4,
    participants: list[ParticipantCreate],
    db: DBSession,
    jwt: UserToken,
    _: ProjectEditor,
) -> list[ParticipantPublic]:
    return db.participants.add_participants(project_id, participants)


@router.post("/projects/{project_id}/participants/upload")
async def upload_participants(
    project_id: UUID4,
    file: UploadFile,
    db: DBSession,
    jwt: UserToken,
    _: ProjectEditor,
) -> list[ParticipantPublic]:
    content = await file.read()
    return db.participants.add_participants_from_csv(project_id, content)


@router.patch("/projects/{project_id}/participants/{participant_id}")
async def update_participant(
    project_id: UUID4,
    participant_id: UUID4,
    update: ParticipantUpdate,
    db: DBSession,
    jwt: UserToken,
    _: ProjectEditor,
) -> ParticipantPublic:
    return db.participants.update_participant(participant_id, update)


@router.delete("/projects/{project_id}/participants/{participant_id}")
async def delete_participant(
    project_id: UUID4,
    participant_id: UUID4,
    db: DBSession,
    jwt: UserToken,
    _: ProjectAdmin,
):
    db.participants.remove_participant(participant_id)


@router.delete("/projects/{project_id}/participants")
async def delete_participants(
    project_id: UUID4,
    delete_request: DeleteParticipantsRequest,
    db: DBSession,
    jwt: UserToken,
    _: ProjectAdmin,
):
    db.participants.remove_participants(project_id, delete_request.participant_ids)


SECTION_SEPARATOR = "\n<hr/>\n"
SUBJECT_SEPARATOR = " / "


@router.get("/projects/{project_id}/{language}/participant-email-template")
async def get_participant_email_template(
    project_id: UUID4,
    language: LanguageCode,
    db: DBSession,
    jwt: UserToken,
    _: ProjectViewer,
) -> ParticipantEmailTemplateRequest:
    subject, template = db.projects.get_participant_email_template(project_id, language)
    return ParticipantEmailTemplateRequest(subject=subject, template=template)


@router.put("/projects/{project_id}/{language}/participant-email-template")
async def set_participant_email_template(
    project_id: UUID4,
    language: LanguageCode,
    request: ParticipantEmailTemplateRequest,
    db: DBSession,
    jwt: UserToken,
    _: ProjectEditor,
) -> ParticipantEmailTemplateRequest:
    if request.template is not None:
        try:
            validate_participant_email_template(request.template)
        except TemplateSyntaxError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid Jinja template: {exc.message}",
            ) from exc

    db.projects.set_participant_email_template(
        project_id, language, request.subject, request.template
    )
    return ParticipantEmailTemplateRequest(
        subject=request.subject, template=request.template
    )


def _resolve_email_for_participant(
    participant_lang: LanguageCode | None,
    per_lang_template: tuple[str | None, str | None] | None,
    all_localizations: list[tuple[LanguageCode, str | None, str | None]],
) -> tuple[str, str] | None:
    """Pick (subject, template) for a participant.

    Returns None if no usable template can be assembled.
    """
    if participant_lang is not None and per_lang_template is not None:
        subj, tmpl = per_lang_template
        if tmpl:
            return subj or "", tmpl

    subjects = [s for (_, s, t) in all_localizations if t and s]
    templates = [t for (_, _, t) in all_localizations if t]
    if not templates:
        return None
    return SUBJECT_SEPARATOR.join(subjects), SECTION_SEPARATOR.join(templates)


@router.post("/projects/{project_id}/participants/send-email")
async def send_participant_emails(
    project_id: UUID4,
    payload: SendParticipantEmailRequest,
    request: Request,
    db: DBSession,
    jwt: UserToken,
    _: ProjectEditor,
) -> SendParticipantEmailResponse:
    all_localizations = db.projects.get_participant_email_templates_ordered(project_id)
    if not any(tmpl for (_, _, tmpl) in all_localizations):
        raise HTTPException(
            status_code=404,
            detail="No participant email template configured for this project.",
        )

    project = db.projects.get_project(project_id)

    all_participants = db.participants.get_participants(project_id)
    if payload.participant_ids is not None:
        wanted = set(payload.participant_ids)
        participants = [p for p in all_participants if p.id in wanted]
    else:
        participants = [p for p in all_participants if p.participating]

    by_lang: dict[LanguageCode, tuple[str | None, str | None]] = {
        lang: (subj, tmpl) for (lang, subj, tmpl) in all_localizations
    }

    base_url = str(request.base_url)
    sent: list[UUID4] = []
    skipped: list[UUID4] = []

    for participant in participants:
        if not participant.email or not participant.participating:
            skipped.append(participant.id)
            continue

        per_lang = by_lang.get(participant.lang) if participant.lang else None
        resolved = _resolve_email_for_participant(
            participant.lang, per_lang, all_localizations
        )
        if resolved is None:
            skipped.append(participant.id)
            continue
        subject, template = resolved

        interview_url = f"{base_url}interview?id={project_id}&pid={participant.pid}"
        opt_out_url = f"{base_url}/opt-out/{participant.pid}"

        context = build_template_context(
            name=participant.name,
            email=participant.email,
            pid=participant.pid,
            interview_url=interview_url,
            project_title=project.title,
            opt_out_url=opt_out_url,
        )

        try:
            html_content = render_participant_email_template(template, context)
        except TemplateError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Failed to render template: {exc}",
            ) from exc

        await send_email(
            participant.email,
            subject,
            html_content=html_content,
        )
        sent.append(participant.id)

    return SendParticipantEmailResponse(sent=sent, skipped=skipped)


@router.put("/projects/{project_id}/interviews/{interview_id}/participant")
async def link_participant_to_interview(
    project_id: UUID4,
    interview_id: UUID4,
    link_request: LinkParticipantRequest,
    db: DBSession,
    jwt: UserToken,
    _: ProjectEditor,
):
    db.participants.link_to_interview(interview_id, link_request.participant_id)
