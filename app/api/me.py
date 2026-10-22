"""The signed-in user's own things: what is waiting, and the queues of it.

Everything here is keyed by the caller rather than by a resource in the path,
which is what separates it from the project routers. There is no id to
role-check, so each route scopes itself to what this user may see -- see
`ReportRepository.list_reports`, whose `collaborated_only` exists for exactly
this.

`/me/notifications` is deliberately a bag of counts rather than a single
number: the account menu will grow more of them (unanswered comments, a test
run that finished), and each costs no extra round trip because the dashboard
layout already loads this alongside the user.

Nothing here is a notification *record*. These are counts and queues derived
from state that already exists -- an unread report is a report with no read
row for this user -- so there is no inbox to keep in step, and marking a
report read is what clears it. A kind of notification that cannot be derived,
because nothing durable records that you have not seen it, is what would force
a stored `notification` table; none exists yet.
"""

from typing import Annotated

from fastapi import APIRouter, Query

from ..dependencies import DBSession, DemoToken
from ..schemas.reports import MessageReportRowPublic, ReportFilterParams
from ..schemas.users import DemoBudget, UserNotifications
from ..types import Scope
from .dashboard.synthesize import get_demo_budget

router = APIRouter(prefix="/me", tags=["me"])


@router.get("/notifications")
async def get_notifications(
    db: DBSession,
    jwt: DemoToken,
) -> UserNotifications:
    """The badge counts for this user.

    Both queues are counted, because an admin who also collaborates has work
    in both and one number could only ever point at one of them.

    `DemoToken` is the lowest signed-in scope, so this answers for every
    account. A demo user simply has no projects to have reports in, and gets
    zeros rather than an error.
    """
    is_admin = Scope.ADMIN in {Scope(scope) for scope in jwt.scope.split()}

    return UserNotifications(
        unread_reports=db.reports.unread_count(jwt.user_id, track="owner"),
        # Not counted at all for a non-admin: the platform queue is not theirs
        # to see, let alone to be nudged about.
        unread_platform_reports=(
            db.reports.unread_count(jwt.user_id, track="admin") if is_admin else 0
        ),
    )


@router.get("/demo-budget")
async def get_my_demo_budget(db: DBSession, jwt: DemoToken) -> DemoBudget | None:
    """How much of their synthetic-interview budgets a demo user has left.

    Null for any other scope: only `Scope.DEMO` accounts are limited.
    """
    if jwt.scope != Scope.DEMO:
        return None
    return get_demo_budget(db, jwt.user_id)


@router.get("/reports")
async def get_my_reports(
    db: DBSession,
    jwt: DemoToken,
    filters: Annotated[ReportFilterParams, Query()],
) -> list[MessageReportRowPublic]:
    """Every reported question waiting on this user, across their projects.

    The destination for the account-menu badge, which is counted the same way:
    a project member has reports in several projects at once, so a queue
    scoped to one of them could never be what the badge points at.

    Scoped to projects the caller collaborates on -- there is no project in
    the path to role-check, so the query does the confining. A platform admin
    gets their own collaborations here, not the platform queue: that one is
    `/admin/reports`, and conflating them would put every project's reports
    in front of anyone who happens to be an admin.
    """
    return db.reports.list_reports(
        user_id=jwt.user_id,
        statuses=filters.statuses,
        unread_only=filters.unread_only,
        collaborated_only=True,
    )
