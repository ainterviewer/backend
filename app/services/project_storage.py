"""Filesystem helpers for the media a project owns outside the database.

Project media lives under ``storage/projects/<project_id>/`` (see
``ainterviewer.storage.ProjectStorage``) and is not covered by any of the
database cascades, so anything that duplicates or removes a project has to
deal with the directory tree separately.
"""

import logging
import shutil

from pydantic import UUID4

from ainterviewer.settings import settings as lib_settings

logger = logging.getLogger(__name__)


def copy_email_attachments(src_project_id: UUID4, dst_project_id: UUID4) -> None:
    """Copy a project's participant email attachments onto another project.

    Copies the whole ``email_attachments`` tree, which covers both the
    per-language directories and the ``_reminders/<language>`` ones.
    """
    src = lib_settings.storage.project_storage.email_attachments_path(src_project_id)
    if not any(src.iterdir()):
        return

    dst = lib_settings.storage.project_storage.email_attachments_path(dst_project_id)
    shutil.copytree(src, dst, dirs_exist_ok=True)


def delete_project_storage(project_id: UUID4) -> None:
    """Remove a project's media directory.

    Best effort: the database row is the source of truth, so a failure here
    leaves orphaned files behind rather than blocking the delete.
    """
    base_path = lib_settings.storage.project_storage.base_path
    path = base_path / str(project_id)

    if path.resolve().parent != base_path:
        logger.error("Refusing to delete storage outside %s: %s", base_path, path)
        return

    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError:
        logger.exception("Failed to delete storage for project %s", project_id)
