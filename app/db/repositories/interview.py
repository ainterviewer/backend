import datetime
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, Optional

from pydantic import UUID4
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import NoResultFound

from ainterviewer.interview_guides import Image, InterviewGuide, SurveyItem
from ainterviewer.types import (
    Feedback,
    Interviewer,
    InterviewStatus,
    MessageRole,
    MessageType,
)
from ainterviewer.utils import now

from ..models import (
    IntervieweeCreate,
    IntervieweePublic,
    InterviewPublic,
    MessagePublic,
)
from ..tables import (
    IntervieweeTable,
    InterviewTable,
    MessageTable,
    ProjectTable,
    TaskTable,
)
from ..types import InterviewType
from .base import BaseRepository


class InterviewRepository(BaseRepository):
    """Repository for Interview, Message, and Task operations."""

    # ==================== Interview Methods ====================
    def change_active_to_inactive(self):
        self.session.execute(
            update(InterviewTable)
            .where(InterviewTable.status == InterviewStatus.ACTIVE)
            .values(status=InterviewStatus.INACTIVE)
        )
        self.session.commit()

    def create_interview(
        self,
        project_id: UUID4,
        interview_guide: InterviewGuide,
        interview_type: InterviewType,
        interviewer: Interviewer = Interviewer.AI,
        synthetic: bool = False,
        test: bool = False,
        **kwargs,
    ) -> InterviewPublic:
        interview = InterviewTable(
            project_id=project_id,
            interview_guide=interview_guide,
            type=interview_type,
            interviewer=interviewer,
            **kwargs,
        )

        self.session.add(interview)
        self.session.commit()
        self.session.refresh(interview)
        return InterviewPublic.model_validate(interview)

    def update_interview_guide(
        self,
        project_id: UUID4,
        interview_id: UUID4,
        interview_guide: InterviewGuide,
    ):
        statement = (
            update(InterviewTable)
            .where(
                InterviewTable.project_id == project_id,
                InterviewTable.id == interview_id,
            )
            .values(interview_guide=interview_guide)
        )

        self.session.execute(statement)
        self.session.commit()

    def delete_interviews(
        self,
        project_id: UUID4,
        interview_ids: list[UUID4],
    ):
        # FIXME: Update permissions to collab
        # Check if the project exists and belongs to the folder
        statement = select(ProjectTable).where(
            ProjectTable.id == project_id,
        )
        self.session.execute(statement).scalar_one()

        statement = delete(InterviewTable).where(
            InterviewTable.project_id == project_id,
            InterviewTable.id.in_(interview_ids),
        )
        self.session.execute(statement)

        self.session.commit()

        self._delete_messages(project_id, interview_ids)

    def _delete_messages(self, project_id: UUID4, interview_ids: list[UUID4]):
        # WARNING: Add project ownership validation if this method is made public

        statement = delete(MessageTable).where(
            MessageTable.project_id == project_id,
            MessageTable.interview_id.in_(interview_ids),
        )
        self.session.execute(statement)

        self.session.commit()

    def _get_total_count(self, table, *conditions) -> int:
        statement = select(func.count()).select_from(table).where(*conditions)

        return self.session.execute(statement).scalar_one()

    def get_interviews(
        self,
        project_id: UUID4,
        with_messages: bool = False,
        offset: int | None = None,
        limit: int | None = None,
        sorting_column: str = "created_at",
        sorting_order: Literal["desc", "asc"] = "desc",
        interview_types: list[InterviewType] | None = None,
        created_at: datetime.datetime | None = None,
        completed: bool | None = None,
    ) -> tuple[Sequence[InterviewPublic], int]:
        SORTABLE_COLUMNS = {"created_at", "last_updated", "status", "language", "type"}
        if sorting_column not in SORTABLE_COLUMNS:
            raise ValueError(f"Invalid sort column: {sorting_column}")
        _sorting_col = getattr(InterviewTable, sorting_column)

        table = InterviewTable
        conditions = [InterviewTable.project_id == project_id]

        if interview_types:
            conditions.append(InterviewTable.type.in_(interview_types))

        if created_at is not None:
            conditions.append(InterviewTable.created_at == created_at)

        if completed is not None:
            conditions.append(InterviewTable.is_complete == completed)

        statement = (
            select(table)
            .where(*conditions)
            .order_by(
                _sorting_col.desc() if sorting_order == "desc" else _sorting_col.asc()
            )
            .offset(offset)
            .limit(limit)
        )

        total = self._get_total_count(table, *conditions)

        interviews = self.session.execute(statement).scalars().all()

        if with_messages:
            for interview in interviews:
                interview.messages

        return [
            InterviewPublic.model_validate(interview) for interview in interviews
        ], total

    def get_interview(
        self,
        project_id: UUID4,
        interview_id: UUID4,
        full: bool = False,
        create: bool = False,
    ) -> InterviewPublic:
        try:
            statement = (
                select(InterviewTable)
                .where(InterviewTable.project_id == project_id)
                .where(InterviewTable.id == interview_id)
            )
            interview = self.session.execute(statement).scalar_one()
        except NoResultFound:
            if create:
                interview = InterviewTable(id=interview_id, project_id=project_id)
                self.session.add(interview)
                self.session.commit()
            else:
                raise
        if full:
            interview.messages
            interview.n_messages

        return InterviewPublic.model_validate(interview)

    def update_interview_status(
        self,
        project_id: UUID4,
        interview_id: UUID4,
        status: InterviewStatus | None = None,
        time_spent: int = 0,
    ):
        values = {
            "last_updated": now(),
            "total_time_spent": InterviewTable.total_time_spent + time_spent,
        }
        if status is not None:
            values["status"] = status

        statement = (
            update(InterviewTable)
            .where(InterviewTable.project_id == project_id)
            .where(InterviewTable.id == interview_id)
            .values(**values)
        )
        self.session.execute(statement)
        self.session.commit()

    # ==================== Message Methods ====================

    def insert_message(
        self,
        message_id: int,
        content: str,
        role: MessageRole,
        interview_id: UUID4,
        project_id: UUID4,
        message_type: MessageType = MessageType.TEXT,
        can_answer: bool = True,
        include_in_history: bool = True,
        attachment: Optional[Path] = None,
        survey_item: Optional[SurveyItem] = None,
        image: Optional[Image | list[Image]] = None,
        section: Optional[int] = None,
        main_question: Optional[int] = None,
        sub_question: Optional[int] = None,
        is_introduction: bool = False,
        outro: bool = False,
        timed: bool = False,
        skipped_by_condition: bool = False,
    ) -> int:
        message = MessageTable(
            content=content,
            project_id=project_id,
            message_type=message_type,
            can_answer=can_answer,
            include_in_history=include_in_history,
            attachment=attachment,
            role=role,
            interview_id=interview_id,
            message_id=message_id,
            section=section,
            main_question=main_question,
            sub_question=sub_question,
            image=image,
            survey_item=survey_item,
            is_introduction=is_introduction,
            outro=outro,
            timed=timed,
            skipped_by_condition=skipped_by_condition,
        )
        self.session.add(message)
        self.session.commit()

        return message.message_id

    def save_image(self, image: Image): ...

    def update_feedback(
        self,
        message_id: int,
        interview_id: UUID4,
        feedback: Optional[Feedback],
    ):
        """Updates a message with feedback"""

        statement = (
            update(MessageTable)
            .where(MessageTable.message_id == message_id)
            .where(MessageTable.interview_id == interview_id)
            .values(feedback=feedback)
        )
        self.session.execute(statement)
        self.session.commit()

    def get_messages(
        self,
        interview_id: UUID4,
        project_id: UUID4,
    ) -> list[MessagePublic]:
        statement = (
            select(MessageTable)
            .where(MessageTable.interview_id == interview_id)
            .where(MessageTable.project_id == project_id)
        )
        messages = self.session.execute(statement).scalars().all()
        return [MessagePublic.model_validate(message) for message in messages]

    # ==================== Task Methods ====================

    def insert_task(
        self,
        message_id: int,
        interview_id: UUID4,
        project_id: UUID4,
        task: str,
        reason: Optional[str] = None,
        context: Optional[str] = None,
        content: Optional[str] = None,
        response: Optional[str] = None,
        model: Optional[str] = None,
        time_spend: Optional[int] = None,
    ):
        new_task = TaskTable(
            message_id=message_id,
            interview_id=interview_id,
            project_id=project_id,
            task=task,
            reason=reason,
            context=context,
            content=content,
            response=response,
            model=model,
            time_spend=time_spend,
        )
        self.session.add(new_task)
        self.session.commit()

    # ==================== Interviewee Methods ====================

    def add_interviewee(self, project_id: UUID4, interviewee: IntervieweeCreate):
        self.session.add(
            IntervieweeTable(project_id=project_id, **interviewee.model_dump())
        )
        self.session.commit()

    def get_interviewee(
        self, project_id: UUID4, interview_id: UUID4
    ) -> IntervieweePublic:
        statement = select(IntervieweeTable).where(
            IntervieweeTable.project_id == project_id,
            IntervieweeTable.interview_id == interview_id,
        )
        interviewee = self.session.execute(statement).scalar_one()

        return IntervieweePublic.model_validate(interviewee)
