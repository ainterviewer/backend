import datetime
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import UUID4, BaseModel
from sqlalchemy import case, func, select

from ainterviewer.types import InterviewStatus, MessageRole

from ....db.tables import InterviewTable, MessageTable
from ....db.types import InterviewType
from ....dependencies import DBSession, UserToken

router = APIRouter(prefix="/monitoring", tags=["monitoring"])


class InterviewStatusCount(BaseModel):
    """Count of interviews by status."""

    status: InterviewStatus
    count: int


class InterviewTypeCount(BaseModel):
    """Count of interviews by type."""

    type: InterviewType
    count: int


class DailyInterviewCount(BaseModel):
    """Count of interviews created per day."""

    date: datetime.date
    count: int
    completed_count: int


class MessageRoleCount(BaseModel):
    """Count of messages by role."""

    role: MessageRole
    count: int


class InterviewDurationStats(BaseModel):
    """Statistics about interview duration (time spent)."""

    min_seconds: int
    max_seconds: int
    avg_seconds: float
    median_seconds: float | None = None


class MessageCountStats(BaseModel):
    """Statistics about message counts per interview."""

    min_messages: int
    max_messages: int
    avg_messages: float


class MonitoringStats(BaseModel):
    """Aggregated monitoring statistics for a project."""

    # Basic counts
    total_interviews: int
    total_messages: int
    total_completed_interviews: int
    completion_rate: float

    # Breakdowns
    interviews_by_status: list[InterviewStatusCount]
    interviews_by_type: list[InterviewTypeCount]
    messages_by_role: list[MessageRoleCount]

    # Time series data
    interviews_over_time: list[DailyInterviewCount]

    # Duration/engagement stats
    duration_stats: InterviewDurationStats | None
    message_count_stats: MessageCountStats | None


@router.get(
    "/projects/{project_id}/stats",
    description="Get monitoring statistics for a project's interviews",
)
async def get_project_monitoring_stats(
    project_id: UUID4,
    db: DBSession,
    jwt: UserToken,
    interview_types: Annotated[list[InterviewType] | None, Query()] = None,
    start_date: Annotated[datetime.datetime | None, Query()] = None,
    end_date: Annotated[datetime.datetime | None, Query()] = None,
) -> MonitoringStats:
    """
    Returns summarized statistics for monitoring distributed interviews.

    Includes:
    - Total counts (interviews, messages, completions)
    - Completion rate
    - Breakdown by interview status and type
    - Breakdown of messages by role (user/assistant)
    - Daily interview counts over time
    - Duration statistics (min, max, avg time spent)
    - Message count statistics per interview
    """
    session = db.session

    # Base conditions for filtering
    interview_conditions = [InterviewTable.project_id == project_id]
    message_conditions = [MessageTable.project_id == project_id]

    if interview_types:
        interview_conditions.append(InterviewTable.type.in_(interview_types))

    if start_date:
        interview_conditions.append(InterviewTable.created_at >= start_date)
        message_conditions.append(MessageTable.created_at >= start_date)

    if end_date:
        interview_conditions.append(InterviewTable.created_at <= end_date)
        message_conditions.append(MessageTable.created_at <= end_date)

    # Total interviews count
    total_interviews_stmt = select(func.count(InterviewTable.id)).where(
        *interview_conditions
    )
    total_interviews = session.execute(total_interviews_stmt).scalar() or 0

    # Total completed interviews
    completed_conditions = interview_conditions + [
        InterviewTable.status == InterviewStatus.COMPLETED
    ]
    total_completed_stmt = select(func.count(InterviewTable.id)).where(
        *completed_conditions
    )
    total_completed = session.execute(total_completed_stmt).scalar() or 0

    # Total messages count
    total_messages_stmt = select(func.count(MessageTable.id)).where(*message_conditions)
    total_messages = session.execute(total_messages_stmt).scalar() or 0

    # Completion rate
    completion_rate = (
        (total_completed / total_interviews * 100) if total_interviews > 0 else 0.0
    )

    # Interviews by status
    status_stmt = (
        select(InterviewTable.status, func.count(InterviewTable.id))
        .where(*interview_conditions)
        .group_by(InterviewTable.status)
    )
    status_results = session.execute(status_stmt).all()
    interviews_by_status = [
        InterviewStatusCount(status=status, count=count)
        for status, count in status_results
    ]

    # Interviews by type
    type_stmt = (
        select(InterviewTable.type, func.count(InterviewTable.id))
        .where(*interview_conditions)
        .group_by(InterviewTable.type)
    )
    type_results = session.execute(type_stmt).all()
    interviews_by_type = [
        InterviewTypeCount(type=itype, count=count) for itype, count in type_results
    ]

    # Messages by role
    role_stmt = (
        select(MessageTable.role, func.count(MessageTable.id))
        .where(*message_conditions)
        .group_by(MessageTable.role)
    )
    role_results = session.execute(role_stmt).all()
    messages_by_role = [
        MessageRoleCount(role=role, count=count) for role, count in role_results
    ]

    # Daily interview counts (last 30 days by default, or within date range)
    date_trunc = func.date(InterviewTable.created_at)
    daily_stmt = (
        select(
            date_trunc.label("date"),
            func.count(InterviewTable.id).label("count"),
            func.sum(
                case(
                    (InterviewTable.status == InterviewStatus.COMPLETED, 1),
                    else_=0,
                )
            ).label("completed_count"),
        )
        .where(*interview_conditions)
        .group_by(date_trunc)
        .order_by(date_trunc)
    )
    daily_results = session.execute(daily_stmt).all()
    interviews_over_time = [
        DailyInterviewCount(
            date=row.date,
            count=row.count,
            completed_count=row.completed_count or 0,
        )
        for row in daily_results
    ]

    # Duration statistics (for completed interviews with time spent > 0)
    duration_conditions = interview_conditions + [InterviewTable.total_time_spent > 0]
    duration_stmt = select(
        func.min(InterviewTable.total_time_spent).label("min_seconds"),
        func.max(InterviewTable.total_time_spent).label("max_seconds"),
        func.avg(InterviewTable.total_time_spent).label("avg_seconds"),
    ).where(*duration_conditions)
    duration_result = session.execute(duration_stmt).first()

    duration_stats = None
    if duration_result and duration_result.min_seconds is not None:
        duration_stats = InterviewDurationStats(
            min_seconds=duration_result.min_seconds,
            max_seconds=duration_result.max_seconds,
            avg_seconds=float(duration_result.avg_seconds or 0),
        )

    # Message count statistics per interview
    message_counts_subquery = (
        select(
            MessageTable.interview_id,
            func.count(MessageTable.id).label("msg_count"),
        )
        .where(*message_conditions)
        .group_by(MessageTable.interview_id)
        .subquery()
    )

    msg_stats_stmt = select(
        func.min(message_counts_subquery.c.msg_count).label("min_messages"),
        func.max(message_counts_subquery.c.msg_count).label("max_messages"),
        func.avg(message_counts_subquery.c.msg_count).label("avg_messages"),
    )
    msg_stats_result = session.execute(msg_stats_stmt).first()

    message_count_stats = None
    if msg_stats_result and msg_stats_result.min_messages is not None:
        message_count_stats = MessageCountStats(
            min_messages=msg_stats_result.min_messages,
            max_messages=msg_stats_result.max_messages,
            avg_messages=float(msg_stats_result.avg_messages or 0),
        )

    return MonitoringStats(
        total_interviews=total_interviews,
        total_messages=total_messages,
        total_completed_interviews=total_completed,
        completion_rate=completion_rate,
        interviews_by_status=interviews_by_status,
        interviews_by_type=interviews_by_type,
        messages_by_role=messages_by_role,
        interviews_over_time=interviews_over_time,
        duration_stats=duration_stats,
        message_count_stats=message_count_stats,
    )
