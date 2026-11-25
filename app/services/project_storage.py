"""Where the media a project owns outside the database lives, and the
helpers that copy and remove it.

Project media lives under ``storage/projects/<project_id>/``, the library's
``ProjectStorage.base_path``. The library defines only the root; every
directory in it is defined here, so adding one needs no library release.

None of it is covered by the database cascades, so anything that duplicates
or removes a project has to deal with the directory tree separately.
"""

import logging
import shutil
from collections.abc import Iterable
from pathlib import Path
from uuid import UUID

from pydantic import UUID4

from ainterviewer.constants import LANGUAGE_CODES
from ainterviewer.settings import settings as lib_settings

from .storage import remove_entity_dirs

logger = logging.getLogger(__name__)


def _project_dir(project_id: UUID4, name: str, *, create: bool) -> Path:
    path = lib_settings.storage.project_storage.base_path / str(project_id) / name
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def image_path(project_id: UUID4) -> Path:
    """The guide's images, under the names the editor uploaded them with.
    Created if missing. Served by name (`app/api/dashboard/guide_images.py`)."""
    return _project_dir(project_id, "images", create=True)


def email_attachments_path(project_id: UUID4) -> Path:
    """The participant emails' attachments: a directory per language, and
    the reminders' under ``_reminders/<language>``. Created if missing."""
    return _project_dir(project_id, "email_attachments", create=True)


def video_path(project_id: UUID4) -> Path:
    """The welcome screen's videos, shared by every language. Created if
    missing. The web server serves them at ``/assets/videos/``."""
    return _project_dir(project_id, "videos", create=True)


def qr_code_path(project_id: UUID4) -> Path:
    """The project's cached interview-link QR code. Created if missing."""
    return _project_dir(project_id, "qr_codes", create=True)


def consent_attachments_path(project_id: UUID4) -> Path:
    """Where a project's consent PDFs live, a directory per language.

    Like the email attachments, outside the versioned content: an upload
    reaches respondents of the published version straight away. Not created
    here: a project with no PDFs has no directory, and the respondent routes
    read it without leaving one behind.
    """
    return _project_dir(project_id, "consent_attachments", create=False)


def copy_project_attachments(src_project_id: UUID4, dst_project_id: UUID4) -> None:
    """Copy a project's email and consent attachments onto another project.

    Copies the whole ``email_attachments`` tree, which covers both the
    per-language directories and the ``_reminders/<language>`` ones, and the
    consent PDFs' per-language directories.
    """
    src = email_attachments_path(src_project_id)
    if any(src.iterdir()):
        dst = email_attachments_path(dst_project_id)
        shutil.copytree(src, dst, dirs_exist_ok=True)

    src = consent_attachments_path(src_project_id)
    if src.is_dir():
        shutil.copytree(
            src, consent_attachments_path(dst_project_id), dirs_exist_ok=True
        )


def prune_consent_attachments(project_id: UUID4, keep: Iterable[str]) -> None:
    """Remove the consent PDFs of every language not in `keep`.

    Best effort, like `delete_project_storage`. Only directories named by a
    language code are touched; `keep` decides which of them stay.
    """
    base = consent_attachments_path(project_id)
    if not base.is_dir():
        return
    kept = set(keep)
    for entry in base.iterdir():
        if not entry.is_dir() or entry.name in kept or entry.name not in LANGUAGE_CODES:
            continue
        try:
            shutil.rmtree(entry)
        except OSError:
            logger.exception(
                "Failed to delete %s consent attachments for project %s",
                entry.name,
                project_id,
            )


def delete_project_storage(project_ids: Iterable[UUID]) -> None:
    """Remove the files of projects whose rows were deleted. Their
    interviews' files are separate: see `StorageToDelete`."""
    remove_entity_dirs(
        lib_settings.storage.project_storage.base_path, project_ids, "project"
    )
