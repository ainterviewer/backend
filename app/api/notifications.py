"""What the signed-in user has waiting for them.

One call, deliberately shaped as a bag of counts rather than a single number:
the account menu will grow more of these (unanswered comments on their
projects, a test run that failed), and each one added here costs no extra
round trip because the dashboard layout already loads this alongside the user.

Nothing here is a notification *record*. These are counts derived from the
state that already exists -- an unread report is a report with no read row for
this user -- so there is no inbox to keep in step, and marking a report read is
what clears it.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from ..db.repositories.reports import ReviewTrack
from ..dependencies import DBSession, DemoToken
from ..types import Scope

router = APIRouter(prefix="/me", tags=["notifications"])


class UserNotifications(BaseModel):
    """Counts for the badges in the account menu."""

    #: Still-open reports on questions that this user has not read yet, on
    #: whichever queue is theirs.
    unread_reports: int = 0
    #: Which queue `unread_reports` was counted on, so the client can send the
    #: user to the right place rather than guess from their scope.
    track: ReviewTrack = "owner"


@router.get("/notifications")
async def get_notifications(
    db: DBSession,
    jwt: DemoToken,
) -> UserNotifications:
    """The badge counts for this user.

    A platform admin is counted on the admin track even when they also
    collaborate on projects: the platform queue is the one that is theirs to
    clear, and reporting both would make the badge mean two different things
    at once.

    `DemoToken` is the lowest signed-in scope, so this answers for every
    account. A demo user simply has no projects to have reports in, and gets
    a zero rather than an error.
    """
    track: ReviewTrack = (
        "admin" if Scope.ADMIN in {Scope(s) for s in jwt.scope.split()} else "owner"
    )

    return UserNotifications(
        unread_reports=db.reports.unread_count(jwt.user_id, track=track),
        track=track,
    )
