from pydantic import UUID4
from sqlalchemy import delete, distinct, exists, func, select, update, or_
from sqlalchemy.orm import selectinload

from ainterviewer.types import MessageRole
from ainterviewer.utils import now

from ..models import (
    AnalysisCategoryCreate,
    AnalysisCategoryPublic,
    MessageAnnotationCreate,
    MessageAnnotationPublic,
    MessagePublic,
)
from ..tables import (
    AnalysisCategoryTable,
    AnnotationValueTable,
    MessageAnnotationTable,
    MessageTable,
)
from .base import BaseRepository


class AnalysisRepository(BaseRepository):
    """Repository for AnalysisCategory and MessageAnnotation operations."""

    # ==================== Analysis Category Methods ====================

    def get_analysis_categories(
        self, project_id: UUID4
    ) -> list[AnalysisCategoryPublic]:
        statement = select(AnalysisCategoryTable).where(
            AnalysisCategoryTable.project_id == project_id
        )
        categories = self.session.execute(statement).scalars().all()
        return [
            AnalysisCategoryPublic.model_validate(category) for category in categories
        ]

    def create_analysis_category(
        self, category: AnalysisCategoryCreate
    ) -> AnalysisCategoryPublic:
        new_category = AnalysisCategoryTable(**category.model_dump())
        self.session.add(new_category)
        self.session.commit()
        self.session.refresh(new_category)
        return AnalysisCategoryPublic.model_validate(new_category)

    def update_analysis_category(
        self, category_id: UUID4, category: AnalysisCategoryCreate
    ) -> AnalysisCategoryPublic:
        statement = (
            update(AnalysisCategoryTable)
            .where(AnalysisCategoryTable.id == category_id)
            .values(**category.model_dump())
            .returning(AnalysisCategoryTable)
        )
        existing_category = self.session.execute(statement).scalar_one()
        self.session.commit()
        return AnalysisCategoryPublic.model_validate(existing_category)

    def delete_analysis_category(self, category_id: UUID4):
        statement = select(AnalysisCategoryTable).where(
            AnalysisCategoryTable.id == category_id
        )
        category = self.session.execute(statement).scalar_one()
        self.session.delete(category)
        self.session.commit()

    def _apply_search_filter(
        self,
        statement,
        search_text: str | None,
        exact_match: bool,
        case_sensitive: bool,
    ):
        if search_text:
            if exact_match:
                if case_sensitive:
                    return statement.where(MessageTable.content == search_text)
                else:
                    return statement.where(
                        func.lower(MessageTable.content) == search_text.lower()
                    )
            else:
                if case_sensitive:
                    return statement.where(
                        MessageTable.content.like(f"%{search_text}%")
                    )
                else:
                    return statement.where(
                        MessageTable.content.ilike(f"%{search_text}%")
                    )
        return statement

    def _apply_questions_filter(
        self,
        statement,
        questions: list[tuple[int, int]] | None,
    ):
        if questions:
            question_filters = [
                (MessageTable.section == section)
                & (MessageTable.main_question == main_question)
                for section, main_question in questions
            ]
            if question_filters:
                return statement.where(or_(*question_filters))
        return statement

    def _load_context(
        self,
        statement,
        context_before: bool,
        context_after: bool,
        include_previous_on_user: bool = False,
    ):
        """Fetches related messages, context_before returns all messages
        to and with the previous main_question and after_context returns all
        messages up to the next main question"""

        if not context_before and not context_after and not include_previous_on_user:
            return statement

        # Convert current statement to a subquery to get matched messages
        matched_messages = statement.subquery("matched")

        # Build conditions for the expanded query
        conditions = [
            # Include all originally matched messages
            MessageTable.id.in_(select(matched_messages.c.id))
        ]

        if include_previous_on_user:
            # Include previous message if current message is from user
            conditions.append(
                exists(
                    select(1)
                    .select_from(matched_messages)
                    .where(
                        (matched_messages.c.role == MessageRole.USER)
                        & (MessageTable.project_id == matched_messages.c.project_id)
                        & (MessageTable.interview_id == matched_messages.c.interview_id)
                        & (MessageTable.message_id == matched_messages.c.message_id - 1)
                    )
                )
            )

        if context_before:
            # Include messages from previous main_question
            conditions.append(
                exists(
                    select(1)
                    .select_from(matched_messages)
                    .where(
                        (MessageTable.project_id == matched_messages.c.project_id)
                        & (MessageTable.section == matched_messages.c.section)
                        & (
                            MessageTable.main_question
                            == matched_messages.c.main_question - 1
                        )
                    )
                )
            )

        if context_after:
            # Include messages from next main_question
            conditions.append(
                exists(
                    select(1)
                    .select_from(matched_messages)
                    .where(
                        (MessageTable.project_id == matched_messages.c.project_id)
                        & (MessageTable.section == matched_messages.c.section)
                        & (
                            MessageTable.main_question
                            == matched_messages.c.main_question + 1
                        )
                    )
                )
            )

        # Return new statement with all conditions
        return select(MessageTable).where(or_(*conditions))

    def count_filtered_messages(
        self,
        project_id: UUID4,
        category_ids: list[UUID4] | None = None,
        search_text: str | None = None,
        exact_match: bool = False,
        case_sensitive: bool = False,
        questions: list[tuple[int, int]] | None = None,
    ) -> int:
        statement = select(func.count(distinct(MessageTable.id))).where(
            MessageTable.project_id == project_id
        )

        if category_ids is not None:
            statement = (
                statement.join(MessageTable.annotations)
                .join(MessageAnnotationTable.values)
                .where(AnnotationValueTable.category_id.in_(category_ids))
            )

        statement = self._apply_search_filter(
            statement, search_text, exact_match, case_sensitive
        )
        statement = self._apply_questions_filter(statement, questions)
        return self.session.execute(statement).scalar_one()

    def get_filtered_messages(
        self,
        project_id: UUID4,
        skip: int,
        limit: int,
        context_before: bool = False,
        context_after: bool = False,
        include_previous_on_user: bool = False,
        category_ids: list[UUID4] | None = None,
        search_text: str | None = None,
        exact_match: bool = False,
        case_sensitive: bool = False,
        questions: list[tuple[int, int]] | None = None,
    ) -> list[MessagePublic]:
        statement = select(MessageTable).where(MessageTable.project_id == project_id)

        if category_ids:
            statement = (
                statement.join(MessageTable.annotations)
                .join(MessageAnnotationTable.values)
                .where(AnnotationValueTable.category_id.in_(category_ids))
            )

        statement = self._apply_search_filter(
            statement, search_text, exact_match, case_sensitive
        )
        statement = self._apply_questions_filter(statement, questions)
        statement = self._load_context(
            statement, context_before, context_after, include_previous_on_user
        )

        statement = (
            statement.distinct()
            .offset(skip)
            .limit(limit)
            .options(
                selectinload(MessageTable.annotations).selectinload(
                    MessageAnnotationTable.values
                ),
                selectinload(MessageTable.interview),
            )
        )
        messages = self.session.execute(statement).scalars().all()
        return [MessagePublic.model_validate(message) for message in messages]

    def get_message_context(
        self,
        project_id: UUID4,
        interview_id: UUID4,
        message_id: UUID4,
        context_before: bool = False,
        context_after: bool = False,
    ) -> list[MessagePublic]:
        # First, get the target message to determine its section, main_question, and timestamp
        target_statement = select(MessageTable).where(
            MessageTable.project_id == project_id,
            MessageTable.interview_id == interview_id,
            MessageTable.id == message_id,
        )
        target_message = self.session.execute(target_statement).scalar_one()

        # Build base conditions
        conditions = [
            MessageTable.project_id == project_id,
            MessageTable.interview_id == interview_id,
            MessageTable.section == target_message.section,
            MessageTable.main_question == target_message.main_question,
        ]

        # Add time-based filtering based on context flags
        if not context_before and not context_after:
            # Just return the target message
            conditions.append(MessageTable.id == message_id)
        elif context_before and context_after:
            # Return all messages in the current main_question except the target
            conditions.append(MessageTable.id != message_id)
        elif context_before:
            # Messages before (not including) the target message
            conditions.append(MessageTable.created_at < target_message.created_at)
        elif context_after:
            # Messages after (not including) the target message
            conditions.append(MessageTable.created_at > target_message.created_at)

        # Query for messages
        statement = (
            select(MessageTable)
            .where(*conditions)
            .order_by(MessageTable.created_at)
            .options(
                selectinload(MessageTable.annotations).selectinload(
                    MessageAnnotationTable.values
                ),
                selectinload(MessageTable.interview),
            )
        )
        messages = self.session.execute(statement).scalars().all()

        return [MessagePublic.model_validate(message) for message in messages]

    # ==================== Message Annotation Methods ====================

    def get_message_annotations(
        self, message_id: UUID4
    ) -> list[MessageAnnotationPublic]:
        statement = select(MessageAnnotationTable).where(
            MessageAnnotationTable.message_id == message_id
        )
        annotations = self.session.execute(statement).scalars().all()
        # Ensure values are loaded
        for annotation in annotations:
            annotation.values

        return [
            MessageAnnotationPublic.model_validate(annotation)
            for annotation in annotations
        ]

    def add_message_annotation(
        self, annotation: MessageAnnotationCreate
    ) -> MessageAnnotationPublic:
        # Create annotation (envelope)
        new_annotation = MessageAnnotationTable(
            message_id=annotation.message_id,
            user_id=annotation.user_id,
            comment=annotation.comment,
        )
        self.session.add(new_annotation)
        self.session.flush()

        # Add values
        for value in annotation.values:
            new_value = AnnotationValueTable(
                annotation_id=new_annotation.id,
                category_id=value.category_id,
                value_int=value.value_int,
            )
            self.session.add(new_value)

        self.session.commit()
        self.session.refresh(new_annotation)

        # Ensure values are loaded for response
        new_annotation.values

        return MessageAnnotationPublic.model_validate(new_annotation)

    def update_message_annotation(
        self, annotation_id: UUID4, annotation: MessageAnnotationCreate
    ) -> MessageAnnotationPublic:
        # Update core fields
        statement = (
            update(MessageAnnotationTable)
            .where(MessageAnnotationTable.id == annotation_id)
            .values(comment=annotation.comment, updated_at=now())
        )
        self.session.execute(statement)

        # Replace values (delete all existing, add new)
        # This is simpler and safer than diffing for this use case
        self.session.execute(
            delete(AnnotationValueTable).where(
                AnnotationValueTable.annotation_id == annotation_id
            )
        )

        for value in annotation.values:
            new_value = AnnotationValueTable(
                annotation_id=annotation_id,
                category_id=value.category_id,
                value_int=value.value_int,
            )
            self.session.add(new_value)

        self.session.commit()

        statement = select(MessageAnnotationTable).where(
            MessageAnnotationTable.id == annotation_id
        )
        existing_annotation = self.session.execute(statement).scalar_one()

        # Ensure values are loaded for response
        existing_annotation.values

        return MessageAnnotationPublic.model_validate(existing_annotation)

    def delete_message_annotation(self, annotation_id: UUID4):
        statement = select(MessageAnnotationTable).where(
            MessageAnnotationTable.id == annotation_id
        )
        annotation = self.session.execute(statement).scalar_one()
        self.session.delete(annotation)
        self.session.commit()
