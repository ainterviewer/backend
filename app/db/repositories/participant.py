import csv
import io

from pydantic import UUID4
from sqlalchemy import delete, select, update

from ..models import UNSET, ParticipantCreate, ParticipantPublic, ParticipantUpdate
from ..tables import InterviewTable, ParticipantTable
from ..utils import uuid_to_urlid
from .base import BaseRepository


class ParticipantRepository(BaseRepository):
    """Repository for Participant operations, scoped to projects."""

    def add_participant(
        self,
        project_id: UUID4,
        participant: ParticipantCreate,
    ) -> ParticipantPublic:
        row = ParticipantTable(
            project_id=project_id,
            **participant.model_dump(),
        )
        self.session.add(row)
        self.session.flush()
        if row.pid is None:
            row.pid = uuid_to_urlid(row.id)
        self.session.commit()
        self.session.refresh(row)

        return ParticipantPublic.model_validate(row)

    def add_participants(
        self,
        project_id: UUID4,
        participants: list[ParticipantCreate],
    ) -> list[ParticipantPublic]:
        rows = [
            ParticipantTable(project_id=project_id, **p.model_dump())
            for p in participants
        ]
        self.session.add_all(rows)
        self.session.flush()
        for row in rows:
            if row.pid is None:
                row.pid = uuid_to_urlid(row.id)
        self.session.commit()
        for row in rows:
            self.session.refresh(row)

        return [ParticipantPublic.model_validate(row) for row in rows]

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

    def get_participant(self, participant_id: UUID4) -> ParticipantPublic:
        row = self.session.execute(
            select(ParticipantTable).where(ParticipantTable.id == participant_id)
        ).scalar_one()
        return ParticipantPublic.model_validate(row)

    def get_participants(self, project_id: UUID4) -> list[ParticipantPublic]:
        rows = (
            self.session.execute(
                select(ParticipantTable)
                .where(ParticipantTable.project_id == project_id)
                .order_by(ParticipantTable.created_at.desc())
            )
            .scalars()
            .all()
        )
        return [ParticipantPublic.model_validate(row) for row in rows]

    def update_participant(
        self,
        participant_id: UUID4,
        update_data: ParticipantUpdate,
    ) -> ParticipantPublic:
        values = {
            k: v
            for k, v in update_data.model_dump(exclude_unset=True).items()
            if v is not UNSET
        }

        if values:
            self.session.execute(
                update(ParticipantTable)
                .where(ParticipantTable.id == participant_id)
                .values(**values)
            )
            self.session.commit()

        return self.get_participant(participant_id)

    def opt_out(
        self, participant_pid: str, reason: str | None = None
    ) -> ParticipantPublic:
        statement = (
            update(ParticipantTable)
            .where(ParticipantTable.pid == participant_pid)
            .values(participating=False, opt_out_reason=reason)
            .returning(ParticipantTable)
        )
        participant = self.session.execute(statement).scalar_one()
        self.session.commit()

        return ParticipantPublic.model_validate(participant)

    def remove_participant(self, participant_id: UUID4) -> None:
        self.session.execute(
            delete(ParticipantTable).where(ParticipantTable.id == participant_id)
        )
        self.session.commit()

    def remove_participants(
        self,
        project_id: UUID4,
        participant_ids: list[UUID4],
    ) -> None:
        if not participant_ids:
            return
        self.session.execute(
            delete(ParticipantTable).where(
                ParticipantTable.project_id == project_id,
                ParticipantTable.id.in_(participant_ids),
            )
        )
        self.session.commit()

    def link_to_interview(
        self,
        interview_id: UUID4,
        participant_id: UUID4 | None,
    ) -> None:
        self.session.execute(
            update(InterviewTable)
            .where(InterviewTable.id == interview_id)
            .values(participant_id=participant_id)
        )
        self.session.commit()
