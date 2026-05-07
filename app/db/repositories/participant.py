import csv
import io
import uuid

from pydantic import UUID4
from sqlalchemy import delete, select, update

from ..models import UNSET, ParticipantCreate, ParticipantPublic, ParticipantUpdate
from ..tables import (
    InterviewTable,
    ParticipantTable,
    ProjectParticipantTable,
    ProjectTable,
)
from ..utils import urlid_to_uuid, uuid_to_urlid
from .base import BaseRepository


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
        latest_interview_at=p.latest_interview_at,
        latest_interview_status=p.latest_interview_status,
    )


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
    ) -> list[ParticipantPublic]:
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

        for row in reader:
            data = {
                k.strip().lower(): (v.strip() if isinstance(v, str) else v)
                for k, v in row.items()
                if k is not None and k.strip().lower() in allowed
            }
            data = {k: (v if v else None) for k, v in data.items()}
            participants.append(ParticipantCreate(**data))  # ty:ignore[invalid-argument-type]

        return self.add_participants(project_id, participants)

    def _get_link(self, project_participant_id: UUID4) -> ProjectParticipantTable:
        return self.session.execute(
            select(ProjectParticipantTable).where(
                ProjectParticipantTable.id == project_participant_id
            )
        ).scalar_one()

    def get_participant(self, project_participant_id: UUID4) -> ParticipantPublic:
        return _to_public(self._get_link(project_participant_id))

    def get_participants(self, project_id: UUID4) -> list[ParticipantPublic]:
        rows = (
            self.session.execute(
                select(ProjectParticipantTable)
                .where(ProjectParticipantTable.project_id == project_id)
                .order_by(ProjectParticipantTable.created_at.desc())
            )
            .scalars()
            .all()
        )
        return [_to_public(link) for link in rows]

    def update_participant(
        self,
        project_participant_id: UUID4,
        update_data: ParticipantUpdate,
    ) -> ParticipantPublic:
        """Update the shared Participant row backing this project link.
        Changes propagate to every project in the same folder by design."""
        values = {
            k: v
            for k, v in update_data.model_dump(exclude_unset=True).items()
            if v is not UNSET
        }

        link = self._get_link(project_participant_id)
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
        token = urlid_to_uuid(opt_out_urlid)
        participant = self.session.execute(
            select(ParticipantTable).where(ParticipantTable.opt_out_token == token)
        ).scalar_one()
        participant.participating = False
        participant.opt_out_reason = reason
        self.session.commit()
        self.session.refresh(participant)

        link = self.session.execute(
            select(ProjectParticipantTable)
            .where(ProjectParticipantTable.participant_id == participant.id)
            .order_by(ProjectParticipantTable.created_at.desc())
            .limit(1)
        ).scalar_one()
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

    def remove_participant(self, project_participant_id: UUID4) -> None:
        link = self._get_link(project_participant_id)
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
