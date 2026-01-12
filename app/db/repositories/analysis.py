from pydantic import UUID4
from sqlalchemy import delete, distinct, func, select, or_
from sqlalchemy.orm import selectinload

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
        statement = select(AnalysisCategoryTable).where(
            AnalysisCategoryTable.id == category_id
        )
        existing_category = self.session.execute(statement).scalar_one()

        existing_category.name = category.name
        existing_category.description = category.description
        existing_category.type = category.type
        existing_category.color = category.color
        existing_category.min_value = category.min_value
        existing_category.max_value = category.max_value

        self.session.commit()
        self.session.refresh(existing_category)
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
        statement = select(MessageAnnotationTable).where(
            MessageAnnotationTable.id == annotation_id
        )
        existing_annotation = self.session.execute(statement).scalar_one()

        # Update core fields
        existing_annotation.comment = annotation.comment
        existing_annotation.updated_at = now()

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
        self.session.refresh(existing_annotation)

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
