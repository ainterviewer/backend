"""The files an interview owns outside the database: its respondent's voice
recordings and uploaded images, under the library's
``InterviewStorage.base_path`` (``storage/interviews/<interview_id>/``).

They are the respondent's data as much as the messages are, so they go with
the interview: every route that deletes interviews removes them too.
"""

from collections.abc import Iterable
from uuid import UUID

from ainterviewer.settings import settings as lib_settings

from .storage import remove_entity_dirs


def delete_interview_storage(interview_ids: Iterable[UUID]) -> None:
    """Remove the files of interviews whose rows were deleted."""
    remove_entity_dirs(
        lib_settings.storage.interview_storage.base_path, interview_ids, "interview"
    )
