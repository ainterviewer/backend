"""The files an interview owns outside the database: its respondent's voice
recordings and uploaded images, under the library's
``InterviewStorage.base_path`` (``storage/interviews/<interview_id>/``).

The library only ever passes these files' names along (an `audio_file` from
`IOProtocol.receive_message` to `PersistenceProtocol.insert_message`); the
backend makes, serves and deletes them, so the directories are defined here.

They are the respondent's data as much as the messages are, so they go with
the interview: every route that deletes interviews removes them too.
"""

from collections.abc import Iterable
from pathlib import Path
from uuid import UUID

from ainterviewer.settings import settings as lib_settings

from .storage import remove_entity_dirs


def _interview_dir(interview_id: UUID, name: str) -> Path:
    base = lib_settings.storage.interview_storage.base_path
    path = base / str(interview_id) / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def audio_path(interview_id: UUID) -> Path:
    """The respondent's voice recordings. Created if missing."""
    return _interview_dir(interview_id, "audio")


def image_path(interview_id: UUID) -> Path:
    """Images the respondent uploaded. Created if missing."""
    return _interview_dir(interview_id, "images")


def delete_interview_storage(interview_ids: Iterable[UUID]) -> None:
    """Remove the files of interviews whose rows were deleted."""
    remove_entity_dirs(
        lib_settings.storage.interview_storage.base_path, interview_ids, "interview"
    )
