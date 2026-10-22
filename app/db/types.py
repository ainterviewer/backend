"""SQLAlchemy column types. The domain's enums and value types are in
`app.types`."""

from __future__ import annotations

import datetime
from typing import final

from sqlalchemy.types import VARCHAR, DateTime, TypeDecorator

from ainterviewer.settings import settings as lib_settings


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
