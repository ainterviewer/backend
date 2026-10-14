from __future__ import annotations

import datetime
from enum import StrEnum
from typing import final

from sqlalchemy.types import VARCHAR, DateTime, TypeDecorator

from ainterviewer.settings import settings as lib_settings


class CodeKind(StrEnum):
    """What a code *is*, which decides what applying it to a passage produces.

    ``TAG`` is the ordinary case: the code either applies or it does not.
    ``SCORE`` asks the coder for a number in the code's own range, for codes
    that are degrees of something rather than presences of it. ``GROUP`` is
    neither -- it organises the branch under it and is never applied, which is
    what keeps a parent's count from pretending to include its children's.
    """

    GROUP = "group"
    TAG = "tag"
    SCORE = "score"


#: The colours a codebook starts with: hues for top-level codes, checked
#: pairwise for colour-vision deficiency. A starting point rather than the set
#: -- the palette is part of the codebook document from there on, and an
#: analyst can edit it, add to it and cut it down.
DEFAULT_PALETTE: tuple[str, ...] = (
    "#0f766e",
    "#b45309",
    "#4338ca",
    "#be185d",
    "#0369a1",
    "#4d7c0f",
    "#7c3aed",
    "#a16207",
)


class AccessRequestStatus(StrEnum):
    WAITING = "waiting"
    FULFILLED = "fulfilled"
    DENIED = "denied"


class InterviewType(StrEnum):
    MANUAL_TEST = "manual_test"
    SYNTHETIC_TEST = "synthetic_test"
    DISTRIBUTED = "distributed"


class ContentVersionKind(StrEnum):
    """Why a `projectcontentversion` was written.

    `PUBLISH` and `MANUAL` are saved by someone on purpose and are never
    deleted. The rest are written as a side effect -- `TEST` for the draft a
    test interview ran on, `RESTORE` for the draft a restore replaced, `AUTO`
    for the autosave snapshots of Phase 4 -- and may be pruned once no
    interview points at them (`VersionRepository.prune`).
    """

    PUBLISH = "publish"
    MANUAL = "manual"
    TEST = "test"
    RESTORE = "restore"
    AUTO = "auto"

    @property
    def is_automatic(self) -> bool:
        return self not in (ContentVersionKind.PUBLISH, ContentVersionKind.MANUAL)


class VerificationPurpose(StrEnum):
    EMAIL_VERIFICATION = "email_verification"
    LOGIN = "login"
    PASSWORD_RESET = "password_reset"


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


@final
class AppDateTime(TypeDecorator):
    """A datetime stored as app-time-zone wall clock, handled as aware.

    The columns hold naive Copenhagen time (`ainterviewer.utils.now()` with its
    offset dropped on the way in) -- SQLite has nowhere to keep an offset. Left
    at that, every value read back was naive: the API sent `07:01` with no
    zone, a browser elsewhere read it as its own 07:01, and an aware filter
    bound (`2026-08-21T00:00Z`) lost its offset and was compared as if it were
    Copenhagen time, two hours off.

    So the zone is attached on the way out and honoured on the way in: values
    read back are aware in the app time zone, and an aware value written or
    compared is converted to app wall clock first. A naive value is taken to
    be app time already, which is what everything stored is.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if isinstance(value, datetime.datetime) and value.tzinfo is not None:
            return value.astimezone(lib_settings.tzinfo).replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        if isinstance(value, datetime.datetime) and value.tzinfo is None:
            return value.replace(tzinfo=lib_settings.tzinfo)
        return value
