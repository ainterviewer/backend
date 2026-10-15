"""The guide's collaborative document, as stored (`GuideDocTable`).

The room (`app/collab/rooms.py`) owns the doc while editors are connected;
this is only what it loads and saves, and what publishing asks of it.
"""

from typing import NamedTuple

from pydantic import UUID4
from sqlalchemy import select

from ainterviewer.interview_guides.localized import LocalizedInterviewGuide

from ..tables import GuideDocTable, ProjectContentTable
from .base import BaseRepository


class GuideColumn(NamedTuple):
    """What a guide doc is projected into, and the languages its texts are in."""

    guide: LocalizedInterviewGuide
    revision: int
    languages: tuple[str, ...]
    default_language: str


class GuideDocRepository(BaseRepository):
    def get(self, project_id: UUID4) -> GuideDocTable | None:
        return self.session.execute(
            select(GuideDocTable).where(GuideDocTable.project_id == project_id)
        ).scalar_one_or_none()

    def column(self, project_id: UUID4) -> GuideColumn:
        """The draft guide, its revision and the project's languages."""
        content = self.session.execute(
            select(ProjectContentTable).where(
                ProjectContentTable.project_id == project_id
            )
        ).scalar_one()
        return GuideColumn(
            content.guide,
            content.guide_revision,
            tuple(content.languages),
            content.default_language,
        )

    def errors(self, project_id: UUID4) -> list[str] | None:
        """Why the doc does not make a guide, if it does not."""
        return self.session.execute(
            select(GuideDocTable.errors).where(GuideDocTable.project_id == project_id)
        ).scalar_one_or_none()

    def save(
        self,
        project_id: UUID4,
        state: bytes,
        synced_revision: int,
        guide: LocalizedInterviewGuide | None,
        errors: list[str] | None,
    ) -> int | None:
        """Store the doc's state and, if it makes one, its guide.

        `synced_revision` is the guide revision the doc last agreed with. If
        the column has moved on since (something wrote it outside the doc),
        nothing is written and None returned: the caller merges that write
        into the doc and saves again. Otherwise returns the guide's revision
        now, which is new only if the guide changed. Flushed, not committed.
        """
        content = self.session.execute(
            select(ProjectContentTable).where(
                ProjectContentTable.project_id == project_id
            )
        ).scalar_one()
        if content.guide_revision != synced_revision:
            return None
        if guide is not None and guide.model_dump(mode="json") != (
            content.guide.model_dump(mode="json")
        ):
            content.guide = guide
            self.session.flush()

        row = self.get(project_id)
        if row is None:
            row = GuideDocTable(project_id=project_id)
            self.session.add(row)
        row.state = state
        row.guide_revision = content.guide_revision
        row.errors = errors
        self.session.flush()
        return content.guide_revision
