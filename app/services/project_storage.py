"""Where the media a project owns outside the database lives, and the
helpers that copy and remove it.

Project media lives under ``storage/projects/<project_id>/``, the library's
``ProjectStorage.base_path``. The library defines the root and the media it
reads itself (``image_path``, for guide images); every directory only the
backend uses is defined here, so adding one needs no library release.

None of it is covered by the database cascades, so anything that duplicates
or removes a project has to deal with the directory tree separately.
"""

import shutil
from collections.abc import Iterable
from pathlib import Path
from uuid import UUID

from pydantic import UUID4

from ainterviewer.settings import settings as lib_settings

from .storage import remove_entity_dirs


def _project_dir(project_id: UUID4, name: str, *, create: bool) -> Path:
    path = lib_settings.storage.project_storage.base_path / str(project_id) / name
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


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


def copy_email_attachments(src_project_id: UUID4, dst_project_id: UUID4) -> None:
    """Copy a project's participant email attachments onto another project.

    Copies the whole ``email_attachments`` tree, which covers both the
    per-language directories and the ``_reminders/<language>`` ones.
    """
    src = email_attachments_path(src_project_id)
    if not any(src.iterdir()):
        return

    dst = email_attachments_path(dst_project_id)
    shutil.copytree(src, dst, dirs_exist_ok=True)


def delete_project_storage(project_ids: Iterable[UUID]) -> None:
    """Remove the files of projects whose rows were deleted. Their
    interviews' files are separate: see `StorageToDelete`."""
    remove_entity_dirs(
        lib_settings.storage.project_storage.base_path, project_ids, "project"
    )
