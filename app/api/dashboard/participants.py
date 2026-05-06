import mimetypes
from pathlib import Path
from typing import Annotated

import polars as pl
from fastapi import APIRouter, Body, HTTPException, Request, UploadFile, Response
from jinja2 import TemplateError
from pydantic import UUID4

from ainterviewer.settings import settings as lib_settings
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
    ParticipantEmailTemplateRequest,
    SendParticipantEmailRequest,
)
from ..response_models import (
    ParticipantEmailAttachment,
    SendParticipantEmailResponse,
)

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


@router.get(
    "/projects/{project_id}/participants/export",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}}}},
)
async def export_participants(
    project_id: UUID4,
    db: DBSession,
    jwt: UserToken,
    _: ProjectViewer,
) -> Response:
    csv = pl.DataFrame(
        participant.model_dump(mode="json")
        for participant in db.participants.get_participants(project_id)
    ).write_csv()
    return Response(
        content=csv,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="participants_{project_id}.csv"'
        },
    )


@router.post("/participants/opt-out/{opt_out_token}")
async def opt_out(
    opt_out_token: str,
    db: DBSession,
    reason: Annotated[str | None, Body()] = None,
) -> ParticipantPublic:
    return db.participants.opt_out(opt_out_token, reason)


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


MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # 10MB per file
MAX_TOTAL_ATTACHMENT_BYTES = 25 * 1024 * 1024  # 25MB per language


def _attachments_dir(project_id: UUID4, language: LanguageCode) -> Path:
    base = lib_settings.storage.project_storage.email_attachments_path(project_id)
    path = base / language
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_filename(filename: str | None) -> str:
    if not filename:
        raise HTTPException(status_code=422, detail="Filename is required.")
    name = Path(filename).name
    if (
        not name
        or name in {".", ".."}
        or name != filename.replace("\\", "/").split("/")[-1]
    ):
        raise HTTPException(status_code=422, detail="Invalid filename.")
    if len(name) > 255:
        raise HTTPException(status_code=422, detail="Filename is too long.")
    return name


def _list_attachments(directory: Path) -> list[ParticipantEmailAttachment]:
    if not directory.exists():
        return []
    items: list[ParticipantEmailAttachment] = []
    for entry in sorted(directory.iterdir()):
        if not entry.is_file():
            continue
        mime, _ = mimetypes.guess_type(entry.name)
        items.append(
            ParticipantEmailAttachment(
                filename=entry.name,
                size=entry.stat().st_size,
                content_type=mime,
            )
        )
    return items


@router.get("/projects/{project_id}/{language}/participant-email-attachments")
async def list_participant_email_attachments(
    project_id: UUID4,
    language: LanguageCode,
    db: DBSession,
    jwt: UserToken,
    _: ProjectViewer,
) -> list[ParticipantEmailAttachment]:
    return _list_attachments(_attachments_dir(project_id, language))


@router.post("/projects/{project_id}/{language}/participant-email-attachments")
async def upload_participant_email_attachments(
    project_id: UUID4,
    language: LanguageCode,
    files: list[UploadFile],
    db: DBSession,
    jwt: UserToken,
    _: ProjectEditor,
) -> list[ParticipantEmailAttachment]:
    if not files:
        raise HTTPException(status_code=422, detail="No files provided.")

    directory = _attachments_dir(project_id, language)

    existing_total = sum(f.stat().st_size for f in directory.iterdir() if f.is_file())

    new_payloads: list[tuple[str, bytes]] = []
    incoming_total = 0
    for upload in files:
        name = _safe_filename(upload.filename)
        content = await upload.read()
        if len(content) > MAX_ATTACHMENT_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Attachment '{name}' exceeds the {MAX_ATTACHMENT_BYTES} byte limit.",
            )
        existing_file = directory / name
        if existing_file.exists():
            existing_total -= existing_file.stat().st_size
        incoming_total += len(content)
        new_payloads.append((name, content))

    if existing_total + incoming_total > MAX_TOTAL_ATTACHMENT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Total attachments would exceed the {MAX_TOTAL_ATTACHMENT_BYTES} byte limit.",
        )

    for name, content in new_payloads:
        (directory / name).write_bytes(content)

    return _list_attachments(directory)


@router.delete(
    "/projects/{project_id}/{language}/participant-email-attachments/{filename}"
)
async def delete_participant_email_attachment(
    project_id: UUID4,
    language: LanguageCode,
    filename: str,
    db: DBSession,
    jwt: UserToken,
    _: ProjectEditor,
) -> list[ParticipantEmailAttachment]:
    name = _safe_filename(filename)
    directory = _attachments_dir(project_id, language)
    target = directory / name
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Attachment not found.")
    target.unlink()
    return _list_attachments(directory)


def _read_attachments(
    project_id: UUID4, languages: set[LanguageCode]
) -> list[tuple[str, bytes]]:
    """Load attachment files for the given languages, deduped by filename."""
    seen: dict[str, bytes] = {}
    base = lib_settings.storage.project_storage.email_attachments_path(project_id)
    for lang in languages:
        directory = base / lang
        if not directory.exists():
            continue
        for entry in sorted(directory.iterdir()):
            if entry.is_file() and entry.name not in seen:
                seen[entry.name] = entry.read_bytes()
    return list(seen.items())


def _resolve_email_for_participant(
    participant_lang: LanguageCode | None,
    per_lang_template: tuple[str | None, str | None] | None,
    all_localizations: list[tuple[LanguageCode, str | None, str | None]],
) -> tuple[str, str, set[LanguageCode]] | None:
    """Pick (subject, template, contributing_languages) for a participant.

    Returns None if no usable template can be assembled.
    """
    if participant_lang is not None and per_lang_template is not None:
        subj, tmpl = per_lang_template
        if tmpl:
            return subj or "", tmpl, {participant_lang}

    subjects = [s for (_, s, t) in all_localizations if t and s]
    templates = [t for (_, _, t) in all_localizations if t]
    langs = {lang for (lang, _, t) in all_localizations if t}
    if not templates:
        return None
    return (
        SUBJECT_SEPARATOR.join(subjects),
        SECTION_SEPARATOR.join(templates),
        langs,
    )


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
        subject, template, langs = resolved
        attachments = _read_attachments(project_id, langs)

        interview_url = f"{base_url}interview?id={project_id}&pid={participant.pid}"
        opt_out_token = db.participants.get_opt_out_urlid(participant.id)
        opt_out_url = f"{base_url}opt-out/{opt_out_token}"

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
            attachments=attachments or None,
        )
        sent.append(participant.id)

    return SendParticipantEmailResponse(sent=sent, skipped=skipped)
