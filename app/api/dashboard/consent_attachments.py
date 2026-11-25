"""PDFs shown with the consent, as links under its text.

Stored on disk per language (`project_storage.consent_attachments_path`), not in the content
columns, so they are not versioned: an upload or delete reaches respondents of
the published version at once, and restoring a version does not bring old
files back. The file name is the link's label and its order (alphabetical).

Respondents get their language's PDFs, or, when it has none, the default
language's: a whole set, never a mix of the two. A language's PDFs stay as
long as the draft or the published version has the language
(`prune_consent_attachments`): removing it from the draft must not take them
from respondents of the published version, who still get the language until
the next publish. The public routes answer like
the consent route beside them: a 409 until the project is published, the
draft's languages with `?draft=true` for a viewer or a shared test link.

Only PDFs are accepted, checked by their header as well as their extension.
They are served as `application/pdf` with `nosniff`, so a browser opens them
in its PDF viewer and never as a page of our origin.
"""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import UUID4

from ainterviewer.types import LanguageCode

from ...db.repositories.errors import NotPublishedError
from ...db.repositories.project import ContentSource
from ...dependencies import (
    DBSession,
    DemoToken,
    ProjectEditor,
    ProjectViewer,
    RespondentContent,
)
from ...schemas.content import ConsentAttachmentPublic
from ...services import project_storage
from ..files import checked_language, content_disposition, file_response, safe_filename

router = APIRouter(tags=["projects"])

MAX_CONSENT_ATTACHMENT_BYTES = 10 * 1024 * 1024  # 10MB per file
MAX_TOTAL_CONSENT_ATTACHMENT_BYTES = 25 * 1024 * 1024  # 25MB per language
#: A PDF's header may follow up to this much leading junk (PDF 1.7, H.3).
_PDF_HEADER_WINDOW = 1024


def _directory(project_id: UUID4, language: LanguageCode) -> Path:
    return project_storage.consent_attachments_path(project_id) / checked_language(
        language
    )


def _list(directory: Path) -> list[ConsentAttachmentPublic]:
    if not directory.is_dir():
        return []
    return [
        ConsentAttachmentPublic(filename=entry.name, size=entry.stat().st_size)
        for entry in sorted(directory.iterdir(), key=lambda e: e.name.casefold())
        if entry.is_file()
    ]


def _serve(directory: Path, filename: str) -> FileResponse:
    target = directory / safe_filename(filename)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Attachment not found.")
    return FileResponse(
        target,
        media_type="application/pdf",
        headers={
            "Content-Disposition": content_disposition(target.name, "inline"),
            "X-Content-Type-Options": "nosniff",
        },
    )


def _respondent_directory(
    db: DBSession, project_id: UUID4, language: LanguageCode, source: ContentSource
) -> Path:
    """The directory whose PDFs respondents in `language` get: its own, or
    the default language's when it has none."""
    languages = db.projects.get_available_languages_optimized(project_id, source)
    if language not in {lang["code"] for lang in languages}:
        # Like `get_consent`: a language the project does not offer.
        raise HTTPException(
            status_code=404,
            detail="The project has no localization for that language.",
        )
    directory = _directory(project_id, language)
    if _list(directory):
        return directory
    default = next(lang["code"] for lang in languages if lang["is_default"])
    return _directory(project_id, default)


def prune_consent_attachments(db: DBSession, project_id: UUID4) -> None:
    """Remove the PDFs of languages neither the draft nor the published
    version has. Call it after anything that can drop a language from
    either: removing one, publishing, restoring a version."""
    keep = {
        lang["code"]
        for lang in db.projects.get_available_languages_optimized(project_id)
    }
    try:
        published = db.projects.get_available_languages_optimized(
            project_id, "published"
        )
    except NotPublishedError:
        published = []
    keep |= {lang["code"] for lang in published}
    project_storage.prune_consent_attachments(project_id, keep)


@router.get("/projects/{project_id}/{language}/consent-attachments")
async def list_consent_attachments(
    project_id: UUID4,
    language: LanguageCode,
    jwt: DemoToken,
    _: ProjectViewer,
) -> list[ConsentAttachmentPublic]:
    """`language`'s own consent PDFs, without the default's fallback."""
    return _list(_directory(project_id, language))


@router.post("/projects/{project_id}/{language}/consent-attachments")
async def upload_consent_attachments(
    project_id: UUID4,
    language: LanguageCode,
    files: list[UploadFile],
    jwt: DemoToken,
    _: ProjectEditor,
) -> list[ConsentAttachmentPublic]:
    """Add PDFs to `language`'s consent; one with the name of an existing
    file replaces it. Respondents see them at once: they are not versioned."""
    if not files:
        raise HTTPException(status_code=400, detail="No files provided.")

    directory = _directory(project_id, language)
    existing = {f.name: f.stat().st_size for f in directory.glob("*") if f.is_file()}
    # Kept as each file is read, so no more than the limit is ever held in
    # memory however many files a request sends.
    total = sum(existing.values())

    payloads: dict[str, bytes] = {}
    for upload in files:
        name = safe_filename(upload.filename)
        if Path(name).suffix.lower() != ".pdf":
            raise HTTPException(
                status_code=400,
                detail=f"'{name}' is not a PDF. Only PDFs can be attached.",
            )
        content = await upload.read(MAX_CONSENT_ATTACHMENT_BYTES + 1)
        if len(content) > MAX_CONSENT_ATTACHMENT_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"'{name}' is larger than "
                f"{MAX_CONSENT_ATTACHMENT_BYTES // (1024 * 1024)} MB.",
            )
        if b"%PDF-" not in content[:_PDF_HEADER_WINDOW]:
            raise HTTPException(
                status_code=400,
                detail=f"'{name}' is not a PDF. Only PDFs can be attached.",
            )
        # A file of the same name, stored or earlier in this request, is
        # replaced, so its size no longer counts.
        replaced = payloads.get(name)
        total += len(content) - (
            len(replaced) if replaced is not None else existing.get(name, 0)
        )
        if total > MAX_TOTAL_CONSENT_ATTACHMENT_BYTES:
            raise HTTPException(
                status_code=413,
                detail="The consent's attachments in this language would be "
                f"larger than {MAX_TOTAL_CONSENT_ATTACHMENT_BYTES // (1024 * 1024)} MB.",
            )
        payloads[name] = content

    directory.mkdir(parents=True, exist_ok=True)
    for name, content in payloads.items():
        (directory / name).write_bytes(content)

    return _list(directory)


@router.get(
    "/projects/{project_id}/{language}/consent-attachments/{filename}",
    response_class=Response,
    responses=file_response("application/pdf"),
)
async def download_consent_attachment(
    project_id: UUID4,
    language: LanguageCode,
    filename: str,
    jwt: DemoToken,
    _: ProjectViewer,
) -> FileResponse:
    return _serve(_directory(project_id, language), filename)


@router.delete("/projects/{project_id}/{language}/consent-attachments/{filename}")
async def delete_consent_attachment(
    project_id: UUID4,
    language: LanguageCode,
    filename: str,
    jwt: DemoToken,
    _: ProjectEditor,
) -> list[ConsentAttachmentPublic]:
    directory = _directory(project_id, language)
    target = directory / safe_filename(filename)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Attachment not found.")
    target.unlink()
    return _list(directory)


# NOTE: Doesn't require auth, like the consent route: the interview page reads
# it before anyone has an account or an interview.
@router.get("/projects/{project_id}/guide/consent/{language}/attachments")
async def get_respondent_consent_attachments(
    project_id: UUID4,
    language: LanguageCode,
    db: DBSession,
    source: RespondentContent,
) -> list[ConsentAttachmentPublic]:
    """The consent PDFs respondents in `language` get: its own, or the
    default language's when it has none."""
    return _list(_respondent_directory(db, project_id, language, source))


@router.get(
    "/projects/{project_id}/guide/consent/{language}/attachments/{filename}",
    response_class=Response,
    responses=file_response("application/pdf"),
)
async def get_respondent_consent_attachment(
    project_id: UUID4,
    language: LanguageCode,
    filename: str,
    db: DBSession,
    source: RespondentContent,
) -> FileResponse:
    """One of the PDFs `get_respondent_consent_attachments` lists, to open
    in the browser."""
    return _serve(_respondent_directory(db, project_id, language, source), filename)
