import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from ainterviewer.utils import now

from ...dependencies import AdminToken, DBSession
from ...services.admin_stats import build_admin_stats
from ..response_models import AdminStats

router = APIRouter()


@router.get("/stats")
async def get_admin_stats(
    db: DBSession,
    jwt: AdminToken,
    days: Annotated[
        int | None,
        Query(
            ge=1,
            le=3660,
            description="Days of daily series to return, ending today; "
            "all history from the week of the first signup when omitted.",
        ),
    ] = None,
) -> AdminStats:
    today = now().date()
    users = db.stats.users()
    if days is not None:
        start = today - datetime.timedelta(days=days - 1)
    elif users:
        # From the Monday of the first signup's week, so a chart that buckets
        # all of history by week starts on a whole one instead of a fragment
        # that reads as a slow start.
        first = min(user.created_at.date() for user in users)
        start = first - datetime.timedelta(days=first.weekday())
    else:
        start = today
    return build_admin_stats(
        users=users,
        activity=db.stats.activity(),
        interviews=db.stats.interviews_per_day(start),
        projects=db.stats.projects_per_day(start),
        start=start,
        today=today,
    )
