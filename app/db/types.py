from enum import StrEnum
from typing import final

from sqlalchemy.types import VARCHAR, TypeDecorator


class CollaboratorRole(StrEnum):
    VIEWER = "viewer"
    ANNOTATOR = "annotator"
    EDITOR = "editor"
    ADMIN = "admin"


class AnnotationType(StrEnum):
    TAG = "tag"
    SCORE = "score"


class AccessRequestStatus(StrEnum):
    WAITING = "waiting"
    FULFILLED = "fulfilled"
    DENIED = "denied"


class InterviewType(StrEnum):
    TEST = "test"
    SYNTHETIC = "synthetic"
    DISTRIBUTED = "distributed"


@final
class LanguageType(TypeDecorator):
    """Custom SQLAlchemy type that validates and transforms language codes"""

    impl = VARCHAR(2)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        """Process values going TO the database"""
        if value is not None:
            value = str(value).upper()
            if len(value) != 2:
                raise ValueError(
                    f"Language code must be exactly 2 characters, got: {value}"
                )
            if not value.isalpha():
                raise ValueError(
                    f"Language code must contain only letters, got: {value}"
                )
        return value

    def process_result_value(self, value, dialect):
        """Process values coming FROM the database"""
        if value is not None:
            return value.upper()
        return value
