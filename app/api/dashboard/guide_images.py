"""Guide images, served by name.

A question's image goes to the respondent as its name only
(`OutgoingMessage.image.name`); the browser loads the file from here. The
interview loop never reads it, and no message row stores it.

- Respondents load it through `GET /interview/images/{name}`, with the
  `interview_token` cookie they already hold, so an `<img src>` needs nothing
  more. The name must be an image of that interview's own guide, the copy it
  runs: a republish that drops an image leaves running interviews and their
  transcripts showing it, and knowing a project id gets nobody its images.
  It covers every interview a browser runs -- distributed, manual tests,
  shared test links -- since `create_interview` sets the cookie for all.
- Viewers of the project load any of its images through
  `GET /projects/{project_id}/images/{name}`: the editor's preview and the
  transcripts.

Images are stored under the name the editor uploaded them with
(`upload_image`), in `project_storage.image_path`. They are served only as
an image type known by their extension, with `nosniff` and a sandboxing CSP,
so an uploaded file never runs as a page of our origin (an SVG's scripts
included).
"""

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import UUID4
from sqlalchemy.exc import NoResultFound

from ainterviewer.interview_guides import InterviewGuide

from ...dependencies import DBSession, InterviewCredential, ProjectViewer
from ...services import project_storage
from ..files import file_response, safe_filename

router = APIRouter()

IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
}

#: Names are the editor's, so a re-upload under the same name replaces the
#: file: cache briefly, not for good.
_CACHE_CONTROL = "private, max-age=3600"


def _image_names(guide: InterviewGuide) -> set[str]:
    return {
        question.image.name
        for section in guide.question_sections
        for question in section.questions
        if question.image
    }


def _serve(project_id: UUID4, name: str) -> FileResponse:
    target = project_storage.image_path(project_id) / safe_filename(name)
    media_type = IMAGE_MEDIA_TYPES.get(target.suffix.lower())
    if media_type is None or not target.is_file():
        raise HTTPException(status_code=404, detail="Image not found.")
    return FileResponse(
        target,
        media_type=media_type,
        headers={
            "Cache-Control": _CACHE_CONTROL,
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get(
    "/interview/images/{name}",
    tags=["interviews"],
    response_class=Response,
    responses=file_response(*sorted(set(IMAGE_MEDIA_TYPES.values()))),
)
async def get_interview_image(
    name: str, interview_token: InterviewCredential, db: DBSession
) -> FileResponse:
    """An image of the respondent's own interview guide, by name."""
    try:
        guide = db.interviews.get_interview_guide(
            interview_token.project_id, interview_token.interview_id
        )
    except NoResultFound:
        raise HTTPException(status_code=404, detail="Interview not found.")

    if guide is None or name not in _image_names(guide):
        raise HTTPException(status_code=404, detail="Image not found.")
    return _serve(interview_token.project_id, name)


@router.get(
    "/projects/{project_id}/images/{name}",
    tags=["projects"],
    response_class=Response,
    responses=file_response(*sorted(set(IMAGE_MEDIA_TYPES.values()))),
)
async def get_project_image(
    project_id: UUID4, name: str, _: ProjectViewer
) -> FileResponse:
    """One of the project's guide images, by name."""
    return _serve(project_id, name)
