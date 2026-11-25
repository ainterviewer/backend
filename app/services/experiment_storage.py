"""Where the files an experiment owns outside the database live.

Under the library's ``ExperimentStorage.base_path``, which sets the root
(``storage/experiments/`` unless configured). Like ``project_storage``, the
directories in it are the backend's to define: the library reads none of
them.
"""

from collections.abc import Iterable
from pathlib import Path
from uuid import UUID

from pydantic import UUID4

from ainterviewer.settings import settings as lib_settings

from .storage import remove_entity_dirs


def qr_code_path(experiment_id: UUID4) -> Path:
    """The experiment's cached distribution-link QR code. Created if missing."""
    path = lib_settings.storage.experiment_storage.base_path / str(experiment_id)
    path = path / "qr_codes"
    path.mkdir(parents=True, exist_ok=True)
    return path


def delete_experiment_storage(experiment_ids: Iterable[UUID]) -> None:
    """Remove the files of experiments whose rows were deleted."""
    remove_entity_dirs(
        lib_settings.storage.experiment_storage.base_path, experiment_ids, "experiment"
    )
