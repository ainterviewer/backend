"""The media a database delete leaves behind, and removing it.

Deleting a project, folder or account deletes rows the database cascades to
-- the projects' interviews, the account's experiments -- and their files sit
in directories named by those rows' ids. Once the rows are gone nothing says
which ids they were, so a route gathers them first and removes the files
once the delete has committed:

    files = StorageToDelete.of_projects(db, project_ids)
    await session_manager.end(project_ids)
    db.projects.delete_folder(folder_id)
    files.remove()

Interviews' files are respondents' recordings and images, so leaving them
behind is not only a disk cost. `python -m app.db.cli sweep-storage` removes
what an earlier delete (or a failed removal) left.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ainterviewer.settings import settings as lib_settings

from ..db.tables import ExperimentTable, InterviewTable, ProjectTable
from . import experiment_storage, interview_storage, project_storage

if TYPE_CHECKING:
    from ..db.crud import InterviewDataBase


@dataclass(frozen=True)
class StorageToDelete:
    project_ids: tuple[UUID, ...] = ()
    interview_ids: tuple[UUID, ...] = ()
    experiment_ids: tuple[UUID, ...] = ()

    @classmethod
    def of_projects(
        cls, db: "InterviewDataBase", project_ids: Iterable[UUID]
    ) -> "StorageToDelete":
        """Projects and their interviews."""
        project_ids = tuple(project_ids)
        return cls(
            project_ids=project_ids,
            interview_ids=tuple(db.interviews.get_project_interview_ids(project_ids)),
        )

    @classmethod
    def of_user(cls, db: "InterviewDataBase", user_id: UUID) -> "StorageToDelete":
        """What deleting an account takes: the projects it owns, with their
        interviews, and its experiments."""
        projects = cls.of_projects(db, db.projects.get_owned_project_ids(user_id))
        return cls(
            project_ids=projects.project_ids,
            interview_ids=projects.interview_ids,
            experiment_ids=tuple(db.tests.get_owned_experiment_ids(user_id)),
        )

    def remove(self) -> None:
        """Remove the files. Call it after the delete has committed: a
        delete that fails must not have taken the files with it."""
        interview_storage.delete_interview_storage(self.interview_ids)
        project_storage.delete_project_storage(self.project_ids)
        experiment_storage.delete_experiment_storage(self.experiment_ids)


def orphaned_storage(session: Session) -> StorageToDelete:
    """The directories in media storage whose project, interview or
    experiment no longer exists. Directories not named by an id are not
    ours to judge and are left out.

    The directories are listed before the ids are read: a row is committed
    before its directory is made, so one created meanwhile is still found
    among the ids, and never taken for an orphan.
    """
    storage = lib_settings.storage
    roots = (
        (storage.project_storage.base_path, ProjectTable),
        (storage.interview_storage.base_path, InterviewTable),
        (storage.experiment_storage.base_path, ExperimentTable),
    )
    listed = [(_id_dirs(base), table) for base, table in roots]
    orphans = [
        tuple(sorted(ids - set(session.execute(select(table.id)).scalars()), key=str))
        for ids, table in listed
    ]
    return StorageToDelete(
        project_ids=orphans[0], interview_ids=orphans[1], experiment_ids=orphans[2]
    )


def _id_dirs(base: Path) -> set[UUID]:
    ids = set()
    for entry in base.iterdir() if base.is_dir() else ():
        try:
            entity_id = UUID(entry.name)
        except ValueError:
            continue
        if entry.is_dir() and str(entity_id) == entry.name:
            ids.add(entity_id)
    return ids
