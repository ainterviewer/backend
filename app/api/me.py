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
from pydantic import BaseModel

from ..db.models import MessageReportRowPublic
from ..db.types import ReportStatus
from ..dependencies import DBSession, DemoToken
from ..types import Scope

router = APIRouter(prefix="/me", tags=["me"])


class UserNotifications(BaseModel):
    """Counts for the badges in the account menu.

    One field per queue rather than one number and a label. A platform admin
    who also collaborates on projects has work waiting in *both*, and the two
    are different jobs -- rewording a question in your own guide is not
    reviewing somebody else's for safety. Counting only one of them hid the
    other completely.
    """

    #: Still-open reports on the project track, in projects this user
    #: collaborates on, that they have not read.
    unread_reports: int = 0
    #: The same on the platform track, across every project. Always 0 for a
    #: caller without admin scope -- there is no queue for them to clear.
    unread_platform_reports: int = 0


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


@router.get("/reports")
async def get_my_reports(
    db: DBSession,
    jwt: DemoToken,
    statuses: Annotated[list[ReportStatus] | None, Query()] = None,
    unread_only: Annotated[bool, Query()] = False,
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
        statuses=statuses,
        unread_only=unread_only,
        collaborated_only=True,
    )
