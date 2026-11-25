"""Removing an entity's directory from media storage.

Projects, interviews and experiments each keep their files in a directory
named by their id under a storage root (``ProjectStorage.base_path`` and so
on). None of it is covered by the database cascades, so a delete removes the
rows first and then the directories, through `remove_entity_dirs`.
"""

import logging
import shutil
from collections.abc import Iterable
from pathlib import Path
from uuid import UUID

logger = logging.getLogger(__name__)


def remove_entity_dirs(base_path: Path, entity_ids: Iterable[UUID], kind: str) -> None:
    """Remove the directory of each of `entity_ids` under `base_path`.

    Best effort: the database is the source of truth, so a failure leaves
    files behind (for `python -m app.db.cli sweep-storage`) rather than
    failing a delete whose rows are already gone. A directory that would
    resolve outside `base_path` is refused, never removed.
    """
    root = base_path.resolve()
    for entity_id in entity_ids:
        path = root / str(UUID(str(entity_id)))
        if path.resolve().parent != root:
            logger.error("Refusing to delete storage outside %s: %s", root, path)
            continue
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            pass
        except OSError:
            logger.exception("Failed to delete storage for %s %s", kind, entity_id)
