from __future__ import annotations

from enum import StrEnum
from typing import final

from sqlalchemy.types import VARCHAR, TypeDecorator


class AnnotationType(StrEnum):
    TAG = "tag"
    SCORE = "score"


class AccessRequestStatus(StrEnum):
    WAITING = "waiting"
    FULFILLED = "fulfilled"
    DENIED = "denied"


class InterviewType(StrEnum):
    MANUAL_TEST = "manual_test"
    SYNTHETIC_TEST = "synthetic_test"
    DISTRIBUTED = "distributed"


class VerificationPurpose(StrEnum):
    EMAIL_VERIFICATION = "email_verification"
    LOGIN = "login"


class EmbeddingTask(StrEnum):
    """Which instruction template a stored vector was produced under.

    Only `DOCUMENT` is ever written today, and it means "no instruction at
    all": the embedding model takes its task on the query side, so one
    document-side vector serves retrieval, classification and reranking alike
    (see `app/embed/templates.py`). The column exists so that a symmetric task
    -- clustering or semantic similarity, where there is no query/document
    split and a task-prefixed variant might measurably help -- can be added as
    a second vector per chunk without a migration, and so that a mixed index is
    never searched by accident.
    """

    DOCUMENT = "document"
    CLUSTERING = "clustering"


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
