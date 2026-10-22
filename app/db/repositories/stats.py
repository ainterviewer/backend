import datetime
from typing import NamedTuple
from uuid import UUID

from sqlalchemy import func, select

from ...types import InterviewType, Scope
from ..tables import InterviewTable, ProjectTable, UserActivityTable, UserTable
from .base import BaseRepository


class UserRow(NamedTuple):
    id: UUID
    scope: Scope
    created_at: datetime.datetime
    expires_at: datetime.datetime | None


class ActivityRow(NamedTuple):
    user_id: UUID
    day: datetime.date


class InterviewDayRow(NamedTuple):
    day: datetime.date
    type: InterviewType
    total: int


class ProjectDayRow(NamedTuple):
    day: datetime.date
    total: int


def _day(column):
    # Timestamps are stored as naive app-time wall clock (AppDateTime), so
    # SQLite's date() is the app's calendar day.
    return func.date(column)


class StatsRepository(BaseRepository):
    """Raw rows behind the admin overview (`app/services/admin_stats.py`).

    Users and their activity come back whole: retention needs every cohort's
    history, and at a few hundred users that is a few thousand rows. Interview
    and project counts are grouped by day in SQL, since there are far more of
    them and only the counts are needed.
    """

    def users(self) -> list[UserRow]:
        rows = self.session.execute(
            select(
                UserTable.id,
                UserTable.scope,
                UserTable.created_at,
                UserTable.expires_at,
            )
        )
        return [UserRow(*row) for row in rows]

    def activity(self) -> list[ActivityRow]:
        rows = self.session.execute(
            select(UserActivityTable.user_id, UserActivityTable.day)
        )
        return [ActivityRow(*row) for row in rows]

    def interviews_per_day(self, since: datetime.date) -> list[InterviewDayRow]:
        day = _day(InterviewTable.created_at)
        rows = self.session.execute(
            select(day, InterviewTable.type, func.count())
            .where(day >= since.isoformat())
            .group_by(day, InterviewTable.type)
        )
        return [
            InterviewDayRow(datetime.date.fromisoformat(d), type_, count)
            for d, type_, count in rows
        ]

    def projects_per_day(self, since: datetime.date) -> list[ProjectDayRow]:
        day = _day(ProjectTable.created_at)
        rows = self.session.execute(
            select(day, func.count()).where(day >= since.isoformat()).group_by(day)
        )
        return [
            ProjectDayRow(datetime.date.fromisoformat(d), count) for d, count in rows
        ]
