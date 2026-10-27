import csv
import io
import uuid
from collections.abc import Sequence
from typing import Literal

from pydantic import UUID4
from sqlalchemy import Select, case, delete, exists, func, or_, select, update
from sqlalchemy.exc import NoResultFound
from sqlalchemy.sql.elements import ColumnElement

from ainterviewer.types import InterviewStatus

from ...schemas.participants import (
    ParticipantCreate,
    ParticipantPublic,
    ParticipantUpdate,
)
from ..tables import (
    InterviewTable,
    ParticipantTable,
    ProjectParticipantTable,
    ProjectTable,
)
from ..utils import urlid_to_uuid, uuid_to_urlid
from .base import BaseRepository
from .errors import OptOutTokenError


def _to_public(link: ProjectParticipantTable) -> ParticipantPublic:
    p = link.participant
    return ParticipantPublic(
        id=link.id,
        project_id=link.project_id,
        participant_id=p.id,
        folder_id=p.folder_id,
        name=p.name,
        email=p.email,
        pid=p.pid or "",
        lang=p.lang,
        participating=p.participating,
        created_at=link.created_at,
        latest_interview_at=link.latest_interview_at,
        latest_interview_status=link.latest_interview_status,
    )


#: The columns `get_participant_page` sorts by. Text columns sort
#: case-insensitively and every column puts empty values last, as the
#: client-side table did.
SORTABLE_PARTICIPANT_COLUMNS = frozenset(
    {
        "name",
        "email",
        "pid",
        "participating",
        "created_at",
        "latest_interview_at",
        "latest_interview_status",
    }
)


def _is_blank_row(row: dict[str | None, str | None]) -> bool:
    """True when every cell in a CSV row is empty or whitespace.

    Trailing newlines and separator lines in hand-edited spreadsheets
    otherwise import as participants with no name, email or pid."""
    for value in row.values():
        if isinstance(value, list):  # csv restval for over-long rows
            if any(v and v.strip() for v in value):
                return False
        elif value is not None and value.strip():
            return False
    return True


def _is_blank(data: ParticipantCreate) -> bool:
    """True when a participant carries no identifying field at all.

    `participating` is ignored: it defaults to True, so it is never evidence
    that the caller actually filled the row in."""
    return not any((data.name, data.email, data.pid, data.lang))


class ParticipantRepository(BaseRepository):
    """Repository for Participant operations.

    Participants are folder-scoped (shared profile lives on ParticipantTable);
    a ProjectParticipantTable join row attaches a Participant to a specific
    Project. Adding/updating shared fields therefore propagates to every
    project in the same folder."""

    def _folder_id_for_project(self, project_id: UUID4) -> uuid.UUID:
        return self.session.execute(
            select(ProjectTable.folder_id).where(ProjectTable.id == project_id)
        ).scalar_one()

    def _get_or_create_participant(
        self,
        folder_id: uuid.UUID,
        data: ParticipantCreate,
    ) -> ParticipantTable:
        """Find an existing folder-scoped Participant by pid (when supplied)
        or create a new one. When found, the existing row is left as-is —
        callers can use update_participant to mutate shared fields."""
        if data.pid is not None:
            existing = self.session.execute(
                select(ParticipantTable).where(
                    ParticipantTable.folder_id == folder_id,
                    ParticipantTable.pid == data.pid,
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing

        row = ParticipantTable(
            folder_id=folder_id,
            name=data.name,
            email=data.email,
            pid=data.pid,
            lang=data.lang,
            participating=data.participating,
        )
        self.session.add(row)
        self.session.flush()
        if row.pid is None:
            row.pid = uuid_to_urlid(row.id)
        return row

    def _attach(
        self, project_id: UUID4, participant: ParticipantTable
    ) -> ProjectParticipantTable:
        """Find or create the (project, participant) link row."""
        existing = self.session.execute(
            select(ProjectParticipantTable).where(
                ProjectParticipantTable.project_id == project_id,
                ProjectParticipantTable.participant_id == participant.id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        link = ProjectParticipantTable(
            project_id=project_id, participant_id=participant.id
        )
        self.session.add(link)
        self.session.flush()
        return link

    def add_participant(
        self,
        project_id: UUID4,
        participant: ParticipantCreate,
    ) -> ParticipantPublic:
        folder_id = self._folder_id_for_project(project_id)
        p = self._get_or_create_participant(folder_id, participant)
        link = self._attach(project_id, p)
        self.session.commit()
        self.session.refresh(link)
        return _to_public(link)

    def add_participants(
        self,
        project_id: UUID4,
        participants: list[ParticipantCreate],
    ) -> list[ParticipantPublic]:
        folder_id = self._folder_id_for_project(project_id)
        links: list[ProjectParticipantTable] = []
        for data in participants:
            if _is_blank(data):
                continue
            p = self._get_or_create_participant(folder_id, data)
            links.append(self._attach(project_id, p))
        self.session.commit()
        for link in links:
            self.session.refresh(link)
        return [_to_public(link) for link in links]

    def add_participants_from_csv(
        self,
        project_id: UUID4,
        content: bytes,
    ) -> tuple[list[ParticipantPublic], int]:
        """Import participants from CSV, returning the created rows and the
        number of blank rows that were skipped."""
        text = content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))

        allowed = {
            "name",
            "email",
            "pid",
            "participating",
            "created",
            "latest_interview_at",
            "latest_interview_status",
        }
        participants: list[ParticipantCreate] = []
        skipped = 0

        for row in reader:
            if _is_blank_row(row):
                skipped += 1
                continue
            data = {
                k.strip().lower(): (v.strip() if isinstance(v, str) else v)
                for k, v in row.items()
                if k is not None and k.strip().lower() in allowed
            }
            data = {k: (v if v else None) for k, v in data.items()}
            participants.append(ParticipantCreate(**data))  # ty:ignore[invalid-argument-type]

        return self.add_participants(project_id, participants), skipped

    def resolve_link_by_pid(self, project_id: UUID4, pid: str) -> UUID4:
        """Resolve a public pid to its project_participant.id within the given
        project. Raises NoResultFound if no folder-scoped Participant matches
        the pid, or if that Participant is not attached to this project."""
        folder_id = self._folder_id_for_project(project_id)
        link_id = self.session.execute(
            select(ProjectParticipantTable.id)
            .join(
                ParticipantTable,
                ProjectParticipantTable.participant_id == ParticipantTable.id,
            )
            .where(
                ProjectParticipantTable.project_id == project_id,
                ParticipantTable.folder_id == folder_id,
                ParticipantTable.pid == pid,
            )
        ).scalar_one()
        return link_id

    def link_in_project(self, project_participant_id: UUID4, project_id: UUID4) -> bool:
        return bool(
            self.session.scalar(
                select(
                    exists().where(
                        ProjectParticipantTable.id == project_participant_id,
                        ProjectParticipantTable.project_id == project_id,
                    )
                )
            )
        )

    def _get_link(
        self, project_participant_id: UUID4, project_id: UUID4 | None = None
    ) -> ProjectParticipantTable:
        """The link row. With `project_id`, only if it belongs to that project:
        a link of another project raises NoResultFound like a missing one."""
        statement = select(ProjectParticipantTable).where(
            ProjectParticipantTable.id == project_participant_id
        )
        if project_id is not None:
            statement = statement.where(
                ProjectParticipantTable.project_id == project_id
            )
        return self.session.execute(statement).scalar_one()

    def get_participant(
        self, project_id: UUID4, project_participant_id: UUID4
    ) -> ParticipantPublic:
        return _to_public(self._get_link(project_participant_id, project_id))

    @staticmethod
    def _public_statement() -> Select:
        """The columns of a `ParticipantPublic`, selected rather than loaded.

        `_to_public` reads the participant and every interview of each link
        through lazy relationships, which costs two queries a row; a page or
        an export reads them from these expressions in one.
        """
        return select(
            ProjectParticipantTable.id,
            ProjectParticipantTable.project_id,
            ParticipantTable.id.label("participant_id"),
            ParticipantTable.folder_id,
            ParticipantTable.name,
            ParticipantTable.email,
            func.coalesce(ParticipantTable.pid, "").label("pid"),
            ParticipantTable.lang,
            ParticipantTable.participating,
            ProjectParticipantTable.created_at,
            ProjectParticipantTable.latest_interview_at.label("latest_interview_at"),
            ProjectParticipantTable.latest_interview_status.label(
                "latest_interview_status"
            ),
        ).join(
            ParticipantTable,
            ProjectParticipantTable.participant_id == ParticipantTable.id,
        )

    @staticmethod
    def _participant_filters(
        project_id: UUID4,
        participant_ids: Sequence[UUID4] | None = None,
        search: str | None = None,
        participating: bool | None = None,
        statuses: list[InterviewStatus] | None = None,
    ) -> dict[str, ColumnElement[bool]]:
        """The conditions for a filter set, keyed by the facet each narrows.

        Keyed so `get_participant_facets` can count a facet with every filter
        but its own applied.
        """
        filters: dict[str, ColumnElement[bool]] = {
            "project": ProjectParticipantTable.project_id == project_id
        }
        if participant_ids is not None:
            filters["ids"] = ProjectParticipantTable.id.in_(participant_ids)
        if participating is not None:
            filters["participating"] = ParticipantTable.participating == participating
        if statuses:
            filters["status"] = ProjectParticipantTable.latest_interview_status.in_(
                statuses
            )
        if search and (term := search.strip()):
            pattern = f"%{term}%"
            # ilike() degrades to lower(x) LIKE lower(y) on SQLite.
            filters["search"] = or_(
                ParticipantTable.name.ilike(pattern),
                ParticipantTable.email.ilike(pattern),
                ParticipantTable.pid.ilike(pattern),
            )
        return filters

    def get_participants(self, project_id: UUID4, **filters) -> list[ParticipantPublic]:
        """Every participant matching the filters, newest first.

        Takes the keywords of `_participant_filters`; with none, the whole
        project.
        """
        conditions = self._participant_filters(project_id, **filters).values()
        statement = (
            self._public_statement()
            .where(*conditions)
            .order_by(ProjectParticipantTable.created_at.desc())
        )
        return [
            ParticipantPublic.model_validate(row._mapping)
            for row in self.session.execute(statement).all()
        ]

    def get_participant_page(
        self,
        project_id: UUID4,
        offset: int | None = None,
        limit: int | None = None,
        sorting_column: str = "created_at",
        sorting_order: Literal["desc", "asc"] = "desc",
        **filters,
    ) -> tuple[list[ParticipantPublic], int]:
        """One page of participants, plus the total matching count."""
        if sorting_column not in SORTABLE_PARTICIPANT_COLUMNS:
            raise ValueError(f"Invalid sort column: {sorting_column}")

        sort_columns = {
            "name": func.lower(ParticipantTable.name),
            "email": func.lower(ParticipantTable.email),
            "pid": func.lower(ParticipantTable.pid),
            "participating": ParticipantTable.participating,
            "created_at": ProjectParticipantTable.created_at,
            "latest_interview_at": ProjectParticipantTable.latest_interview_at,
            "latest_interview_status": ProjectParticipantTable.latest_interview_status,
        }
        column = sort_columns[sorting_column]
        direction = column.desc() if sorting_order == "desc" else column.asc()

        conditions = list(self._participant_filters(project_id, **filters).values())
        statement = (
            self._public_statement()
            .where(*conditions)
            # The id breaks ties, so a page boundary cannot fall inside a run
            # of equal values and show a row twice or never.
            .order_by(direction.nulls_last(), ProjectParticipantTable.id)
            .offset(offset)
            .limit(limit)
        )
        count_statement = (
            select(func.count())
            .select_from(ProjectParticipantTable)
            .join(
                ParticipantTable,
                ProjectParticipantTable.participant_id == ParticipantTable.id,
            )
            .where(*conditions)
        )

        total = self.session.execute(count_statement).scalar_one()
        rows = self.session.execute(statement).all()
        return [ParticipantPublic.model_validate(row._mapping) for row in rows], total

    def get_participant_ids(self, project_id: UUID4, **filters) -> list[UUID4]:
        """Every link id `get_participant_page` would page through, through the
        same filters, so a bulk action on "all matching" covers exactly the
        rows the list counted."""
        conditions = self._participant_filters(project_id, **filters).values()
        statement = (
            select(ProjectParticipantTable.id)
            .join(
                ParticipantTable,
                ProjectParticipantTable.participant_id == ParticipantTable.id,
            )
            .where(*conditions)
        )
        return list(self.session.execute(statement).scalars())

    def get_participant_facets(
        self, project_id: UUID4, **filters
    ) -> dict[str, dict[str, int]]:
        """Distinct values and their counts for each filterable column, each
        counted with every *other* filter applied.

        Participants with no interview have no status and are left out of
        that facet: there is no status to filter them by.
        """
        conditions = self._participant_filters(project_id, **filters)
        columns = {
            "participating": case(
                (ParticipantTable.participating, "true"), else_="false"
            ),
            "status": ProjectParticipantTable.latest_interview_status,
        }

        facets: dict[str, dict[str, int]] = {}
        for facet, column in columns.items():
            statement = (
                select(column, func.count())
                .select_from(ProjectParticipantTable)
                .join(
                    ParticipantTable,
                    ProjectParticipantTable.participant_id == ParticipantTable.id,
                )
                .where(*(c for key, c in conditions.items() if key != facet))
                .group_by(column)
            )
            facets[facet] = {
                str(value): count
                for value, count in self.session.execute(statement).all()
                if value is not None
            }
        return facets

    def update_participant(
        self,
        project_id: UUID4,
        project_participant_id: UUID4,
        update_data: ParticipantUpdate,
    ) -> ParticipantPublic:
        """Update the shared Participant row backing this project link.
        Changes propagate to every project in the same folder by design."""
        values = update_data.model_dump()

        link = self._get_link(project_participant_id, project_id)
        if values:
            self.session.execute(
                update(ParticipantTable)
                .where(ParticipantTable.id == link.participant_id)
                .values(**values)
            )
            self.session.commit()
            self.session.refresh(link)

        return _to_public(link)

    def opt_out(
        self, opt_out_urlid: str, reason: str | None = None
    ) -> ParticipantPublic:
        """Resolve the opt-out token (URL-safe encoded UUID) to a single
        Participant row and flip its participating flag. Returns the public
        view from any one of the participant's project links (callers using
        the public opt-out endpoint don't have project context)."""
        try:
            token = urlid_to_uuid(opt_out_urlid)
        except ValueError as exc:  # binascii.Error is a ValueError
            raise OptOutTokenError(f"undecodable token: {exc}") from exc

        participant = self.session.execute(
            select(ParticipantTable).where(ParticipantTable.opt_out_token == token)
        ).scalar_one_or_none()
        if participant is None:
            raise OptOutTokenError("token matches no participant")

        participant.participating = False
        participant.opt_out_reason = reason
        self.session.commit()
        self.session.refresh(participant)

        # The opt-out above is already committed; this only shapes the
        # response, which needs a project link to render.
        try:
            link = self.session.execute(
                select(ProjectParticipantTable)
                .where(ProjectParticipantTable.participant_id == participant.id)
                .order_by(ProjectParticipantTable.created_at.desc())
                .limit(1)
            ).scalar_one()
        except NoResultFound as exc:
            raise OptOutTokenError(
                f"participant {participant.id} opted out but has no project link"
            ) from exc
        return _to_public(link)

    def get_opt_out_urlid(self, project_participant_id: UUID4) -> str:
        link = self._get_link(project_participant_id)
        return uuid_to_urlid(link.participant.opt_out_token)

    def _delete_participant_if_orphan(self, participant_id: uuid.UUID) -> None:
        remaining = self.session.execute(
            select(ProjectParticipantTable.id).where(
                ProjectParticipantTable.participant_id == participant_id
            )
        ).first()
        if remaining is None:
            self.session.execute(
                delete(ParticipantTable).where(ParticipantTable.id == participant_id)
            )

    def remove_participant(
        self, project_id: UUID4, project_participant_id: UUID4
    ) -> None:
        link = self._get_link(project_participant_id, project_id)
        participant_id = link.participant_id
        self.session.execute(
            delete(ProjectParticipantTable).where(
                ProjectParticipantTable.id == project_participant_id
            )
        )
        self._delete_participant_if_orphan(participant_id)
        self.session.commit()

    def remove_participants(
        self,
        project_id: UUID4,
        project_participant_ids: list[UUID4],
    ) -> None:
        if not project_participant_ids:
            return
        participant_ids = (
            self.session.execute(
                select(ProjectParticipantTable.participant_id).where(
                    ProjectParticipantTable.project_id == project_id,
                    ProjectParticipantTable.id.in_(project_participant_ids),
                )
            )
            .scalars()
            .all()
        )
        self.session.execute(
            delete(ProjectParticipantTable).where(
                ProjectParticipantTable.project_id == project_id,
                ProjectParticipantTable.id.in_(project_participant_ids),
            )
        )
        for pid in set(participant_ids):
            self._delete_participant_if_orphan(pid)
        self.session.commit()

    def link_to_interview(
        self,
        interview_id: UUID4,
        project_participant_id: UUID4 | None,
    ) -> None:
        self.session.execute(
            update(InterviewTable)
            .where(InterviewTable.id == interview_id)
            .values(participant_id=project_participant_id)
        )
        self.session.commit()
