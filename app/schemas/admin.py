"""The platform's own administration: users, access, statistics."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import UUID4, model_validator

from ..types import Scope
from .common import Schema


class ScopeCounts(Schema):
    """A count split by account scope, so the overview can hide demo accounts
    (invited in batches that would swamp real growth) without a refetch."""

    admin: int = 0
    user: int = 0
    demo: int = 0


class InterviewTypeCounts(Schema):
    distributed: int = 0
    manual_test: int = 0
    synthetic_test: int = 0


class DailyStats(Schema):
    """One calendar day (app time zone) of the admin overview.

    `accounts` and `usable` are of accounts that still exist: a deleted user
    leaves no trace, so the history shrinks with them.
    """

    day: date
    signups: ScopeCounts
    # Accounts existing at the end of the day.
    accounts: ScopeCounts
    # Of those, the ones whose `expires_at` had not passed: what demo expiry
    # takes out of `accounts`.
    usable: ScopeCounts
    # Distinct users active that day, and over the 7 days ending on it.
    active: ScopeCounts
    weekly_active: ScopeCounts
    interviews: InterviewTypeCounts
    projects: int


class AdminStatsSummary(Schema):
    """Headline figures as of today, independent of the requested range."""

    accounts: ScopeCounts
    usable: ScopeCounts
    active_today: ScopeCounts
    active_7d: ScopeCounts
    active_30d: ScopeCounts
    new_7d: ScopeCounts
    new_30d: ScopeCounts


class RetentionWeek(Schema):
    """Of a cohort's `eligible` members -- those whose week `week` after
    signing up has fully passed -- how many were active during it."""

    week: int
    eligible: int
    retained: int


class RetentionCohort(Schema):
    """The users of one scope who signed up in one month (`YYYY-MM`)."""

    month: str
    scope: Scope
    size: int
    weeks: list[RetentionWeek]


class AdminStats(Schema):
    start: date
    end: date
    summary: AdminStatsSummary
    days: list[DailyStats]
    retention: list[RetentionCohort]


class AccessRequestsProcessRequest(Schema):
    ids: list[UUID4]
    scopes: list[Scope]
    action: Literal["approve", "deny"]

    @model_validator(mode="after")
    def validate_data(self):
        assert len(self.ids) == len(self.scopes)

        return self


class AccessRequestsDeleteRequest(Schema):
    ids: list[UUID4]


class InvitationsDeleteRequest(Schema):
    ids: list[UUID4]


class AdminNoteUpdate(Schema):
    note: str | None = None
