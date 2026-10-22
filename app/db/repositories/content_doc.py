"""The setup pages' collaborative documents, as stored (`ContentDocTable`).

The rooms (`app/collab/rooms.py`) own the docs while editors are connected;
this is only what they load and save, and what publishing asks of them.
"""

from typing import Any, NamedTuple

from pydantic import UUID4, BaseModel
from sqlalchemy import select

from ...types import ContentPart
from ..tables import ContentDocTable, ProjectContentTable
from .base import BaseRepository


class PartColumn(NamedTuple):
    """What a part's doc is projected into, and the languages its texts are in."""

    #: The column's value: a guide, a consent, a welcome, or None.
    value: Any
    revision: int
    languages: tuple[str, ...]
    default_language: str


def _dump(value: BaseModel | None) -> Any:
    return value.model_dump(mode="json") if value is not None else None


class ContentDocRepository(BaseRepository):
    def get(self, project_id: UUID4, part: ContentPart) -> ContentDocTable | None:
        return self.session.execute(
            select(ContentDocTable).where(
                ContentDocTable.project_id == project_id,
                ContentDocTable.part == part,
            )
        ).scalar_one_or_none()

    def _content(self, project_id: UUID4) -> ProjectContentTable:
        return self.session.execute(
            select(ProjectContentTable).where(
                ProjectContentTable.project_id == project_id
            )
        ).scalar_one()

    def column(self, project_id: UUID4, part: ContentPart) -> PartColumn:
        """The draft's `part`, its revision and the project's languages."""
        content = self._content(project_id)
        return PartColumn(
            getattr(content, part.value),
            getattr(content, f"{part.value}_revision"),
            tuple(content.languages),
            content.default_language,
        )

    def errors(self, project_id: UUID4) -> dict[ContentPart, list[str]]:
        """Per part, why its doc does not make a valid one, where it does not.

        Only a doc still in step with its column counts. One the column has
        moved past since -- a restore, a PUT, written with no room open --
        becomes that column's value when its room next opens (the column wins
        the merge wherever the two differ), so its errors no longer describe
        anything, and must not hold back publishing the valid draft."""
        content = self._content(project_id)
        rows = self.session.execute(
            select(
                ContentDocTable.part, ContentDocTable.errors, ContentDocTable.revision
            ).where(
                ContentDocTable.project_id == project_id,
                ContentDocTable.errors.is_not(None),
            )
        )
        return {
            part: errors
            for part, errors, revision in rows
            if errors and revision == content.revision_of(part)
        }

    def save(
        self,
        project_id: UUID4,
        part: ContentPart,
        state: bytes,
        synced_revision: int,
        value: BaseModel | None,
        errors: list[str] | None,
    ) -> int | None:
        """Store the doc's state and, unless it has `errors`, its `value`.

        `synced_revision` is the part's revision the doc last agreed with. If
        the column has moved on since (something wrote it outside the doc),
        nothing is written and None returned: the caller merges that write
        into the doc and saves again. Otherwise returns the part's revision
        now, which is new only if the value changed. Flushed, not committed.
        """
        content = self._content(project_id)
        if getattr(content, f"{part.value}_revision") != synced_revision:
            return None
        if not errors and _dump(value) != _dump(getattr(content, part.value)):
            setattr(content, part.value, value)
            self.session.flush()

        row = self.get(project_id, part)
        if row is None:
            row = ContentDocTable(project_id=project_id, part=part)
            self.session.add(row)
        row.state = state
        row.revision = getattr(content, f"{part.value}_revision")
        row.errors = errors
        self.session.flush()
        return row.revision
