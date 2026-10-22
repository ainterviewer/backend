"""The admin overview's figures, aggregated from `StatsRepository`'s rows.

Pure functions over plain rows, so the arithmetic -- cumulative accounts,
rolling weekly windows, retention eligibility -- is testable without a
database. Every day is a calendar day in the app time zone, which is what
`user_activity.day` and SQLite's `date()` on stored timestamps both give.
"""

import datetime
from collections import Counter, defaultdict
from collections.abc import Iterable
from uuid import UUID

from ..db.repositories.stats import ActivityRow, InterviewDayRow, ProjectDayRow, UserRow
from ..schemas.admin import (
    AdminStats,
    AdminStatsSummary,
    DailyStats,
    InterviewTypeCounts,
    RetentionCohort,
    RetentionWeek,
    ScopeCounts,
)
from ..types import Scope

# Weeks after signup at which retention is reported. Week 0 is left out: the
# signup day itself is an active day, so it would read 100% for every cohort.
RETENTION_WEEKS = (1, 2, 4, 8, 12)


def _counts(scopes: Iterable[Scope]) -> ScopeCounts:
    counter = Counter(scopes)
    return ScopeCounts(**{scope.value: counter[scope] for scope in Scope})


def _dates(start: datetime.date, end: datetime.date) -> list[datetime.date]:
    return [start + datetime.timedelta(days=i) for i in range((end - start).days + 1)]


def _usable(user: UserRow, day: datetime.date) -> bool:
    """Whether ``user`` existed and had not expired at the end of ``day``."""
    if user.created_at.date() > day:
        return False
    return user.expires_at is None or user.expires_at.date() > day


def _active_between(
    active_days: dict[UUID, set[datetime.date]],
    scope_of: dict[UUID, Scope],
    start: datetime.date,
    end: datetime.date,
) -> ScopeCounts:
    return _counts(
        scope_of[user_id]
        for user_id, days in active_days.items()
        if any(start <= day <= end for day in days)
    )


def _retention(
    users: list[UserRow],
    active_days: dict[UUID, set[datetime.date]],
    today: datetime.date,
) -> list[RetentionCohort]:
    cohorts: dict[tuple[str, Scope], list[UserRow]] = defaultdict(list)
    for user in users:
        cohorts[(user.created_at.strftime("%Y-%m"), user.scope)].append(user)

    result = []
    for (month, scope), members in sorted(cohorts.items()):
        weeks = []
        for week in RETENTION_WEEKS:
            eligible = retained = 0
            for user in members:
                start = user.created_at.date() + datetime.timedelta(weeks=week)
                end = start + datetime.timedelta(days=6)
                # Only a week that is over can say someone did not come back.
                if end >= today:
                    continue
                eligible += 1
                if any(start <= day <= end for day in active_days.get(user.id, ())):
                    retained += 1
            weeks.append(RetentionWeek(week=week, eligible=eligible, retained=retained))
        result.append(
            RetentionCohort(month=month, scope=scope, size=len(members), weeks=weeks)
        )
    return result


def build_admin_stats(
    *,
    users: list[UserRow],
    activity: list[ActivityRow],
    interviews: list[InterviewDayRow],
    projects: list[ProjectDayRow],
    start: datetime.date,
    today: datetime.date,
) -> AdminStats:
    """The overview from ``start`` to ``today``, both inclusive.

    The summary and retention look at all history whatever ``start`` is; only
    the daily series is cut to the range. Activity of users that no longer
    exist is ignored (it cannot be attributed to a scope).
    """
    scope_of = {user.id: user.scope for user in users}
    active_days: dict[UUID, set[datetime.date]] = defaultdict(set)
    active_on: dict[datetime.date, set[UUID]] = defaultdict(set)
    for row in activity:
        if row.user_id in scope_of:
            active_days[row.user_id].add(row.day)
            active_on[row.day].add(row.user_id)

    # Accounts and usable accounts are running sums, seeded with everything
    # before `start`: a user stops being usable on the day they expire, or on
    # the day they signed up if they were created already expired.
    signups_on: dict[datetime.date, list[Scope]] = defaultdict(list)
    expiries_on: dict[datetime.date, list[Scope]] = defaultdict(list)
    accounts: Counter[Scope] = Counter()
    usable: Counter[Scope] = Counter()
    for user in users:
        created = user.created_at.date()
        expired = None
        if user.expires_at is not None:
            expired = max(created, user.expires_at.date())
        if created < start:
            accounts[user.scope] += 1
            usable[user.scope] += 1
        else:
            signups_on[created].append(user.scope)
        if expired is not None:
            if expired < start:
                usable[user.scope] -= 1
            else:
                expiries_on[expired].append(user.scope)

    interviews_on: dict[datetime.date, InterviewTypeCounts] = defaultdict(
        InterviewTypeCounts
    )
    for row in interviews:
        counts = interviews_on[row.day]
        setattr(counts, row.type.value, getattr(counts, row.type.value) + row.total)
    projects_on = {row.day: row.total for row in projects}

    week = datetime.timedelta(days=6)
    days = []
    for day in _dates(start, today):
        signups = signups_on.get(day, [])
        accounts.update(signups)
        usable.update(signups)
        usable.subtract(expiries_on.get(day, []))
        weekly: set[UUID] = set()
        for offset in range(7):
            weekly |= active_on.get(day - datetime.timedelta(days=offset), set())
        days.append(
            DailyStats(
                day=day,
                signups=_counts(signups),
                accounts=ScopeCounts(**{s.value: accounts[s] for s in Scope}),
                usable=ScopeCounts(**{s.value: usable[s] for s in Scope}),
                active=_counts(scope_of[user_id] for user_id in active_on.get(day, ())),
                weekly_active=_counts(scope_of[user_id] for user_id in weekly),
                interviews=interviews_on.get(day, InterviewTypeCounts()),
                projects=projects_on.get(day, 0),
            )
        )

    def joined_since(since: datetime.date) -> ScopeCounts:
        return _counts(user.scope for user in users if user.created_at.date() >= since)

    summary = AdminStatsSummary(
        accounts=_counts(user.scope for user in users),
        usable=_counts(user.scope for user in users if _usable(user, today)),
        active_today=_active_between(active_days, scope_of, today, today),
        active_7d=_active_between(active_days, scope_of, today - week, today),
        active_30d=_active_between(
            active_days, scope_of, today - datetime.timedelta(days=29), today
        ),
        new_7d=joined_since(today - week),
        new_30d=joined_since(today - datetime.timedelta(days=29)),
    )

    return AdminStats(
        start=start,
        end=today,
        summary=summary,
        days=days,
        retention=_retention(users, active_days, today),
    )
