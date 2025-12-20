from collections.abc import Sequence
from pathlib import Path
from typing import Literal, Optional

from pydantic import UUID4
from sqlalchemy import delete, func, select
from sqlalchemy.exc import NoResultFound

from ainterviewer.interview_guides import Image, InterviewGuide, SurveyItem
from ainterviewer.types import Feedback, Interviewer, MessageRole, MessageType
from ainterviewer.utils import now

from ..models import IntervieweeCreate, InterviewPublic, MessagePublic
from ..tables import (
    IntervieweeTable,
    InterviewTable,
    MessageTable,
    ProjectTable,
    TaskTable,
)
from .base import BaseRepository


class InterviewRepository(BaseRepository):
    """Repository for Interview, Message, and Task operations."""

    # ==================== Interview Methods ====================

    def create_interview(
        self,
        project_id: UUID4,
        interview_guide: InterviewGuide,
        interviewer: Interviewer = Interviewer.AI,
        synthetic=False,
        test=False,
        **kwargs,
    ) -> InterviewPublic:
        interview = InterviewTable(
            project_id=project_id,
            interview_guide=interview_guide,
            interviewer=interviewer,
            is_synthetic=synthetic,
            is_test=test,
            **kwargs,
        )

        self.session.add(interview)
        self.session.commit()
        self.session.refresh(interview)
        return InterviewPublic.model_validate(interview)

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
        synthetic: bool | None = None,
        test: bool | None = None,
    ) -> tuple[Sequence[InterviewPublic], int]:
        _sorting_col = getattr(InterviewTable, sorting_column)

        table = InterviewTable
        conditions = [InterviewTable.project_id == project_id]

        if synthetic is not None:
            conditions.append(InterviewTable.is_synthetic == synthetic)

        if test is not None:
            conditions.append(InterviewTable.is_test == test)

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
        is_active: Optional[bool] = None,
        is_complete: Optional[bool] = None,
        time_spent: int = 0,
    ):
        statement = (
            select(InterviewTable)
            .where(InterviewTable.project_id == project_id)
            .where(InterviewTable.id == interview_id)
        )
        interview = self.session.execute(statement).scalar_one()
        if is_active is not None:
            interview.is_active = is_active
        if is_complete is not None:
            interview.is_complete = is_complete
        interview.last_updated = now()
        interview.total_time_spent += time_spent
        self.session.add(interview)
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
            select(MessageTable)
            .where(MessageTable.message_id == message_id)
            .where(MessageTable.interview_id == interview_id)
        )
        message = self.session.execute(statement).scalar_one()
        message.feedback = feedback
        self.session.add(message)
        self.session.commit()
        self.session.refresh(message)

    def get_messages(
        self, interview_id: UUID4, project_id: UUID4
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

    def add_interviewee(self, interviewee: IntervieweeCreate):
        self.session.add(IntervieweeTable(**interviewee.model_dump()))
        self.session.commit()
