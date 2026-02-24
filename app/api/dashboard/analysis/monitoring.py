import datetime
import math
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import UUID4, BaseModel
from sqlalchemy import case, func, select

from ainterviewer.types import InterviewStatus

from ....db.tables import InterviewTable, MessageTable
from ....db.types import InterviewType
from ....dependencies import DBSession, UserToken, DemoToken

router = APIRouter(prefix="/monitoring", tags=["monitoring"])


class InterviewStatusCount(BaseModel):
    """Count of interviews by status."""

    status: InterviewStatus
    count: int


class DailyInterviewCount(BaseModel):
    """Count of interviews created per day."""

    date: datetime.date
    count: int
    completed_count: int


class InterviewTimeOfDayCount(BaseModel):
    time: str
    count: int


class InterviewDurationStats(BaseModel):
    """Statistics about interview duration (time spent)."""

    min_seconds: int
    max_seconds: int
    avg_seconds: float
    sum_seconds: float


class MessageCountStats(BaseModel):
    """Statistics about message counts per interview."""

    min_messages: int
    max_messages: int
    avg_messages: float
    sum_messages: int


class HistogramBucket(BaseModel):
    """A value-count pair for histogram use."""

    value: int
    count: int
    label: str


def _compute_histogram_buckets(
    data_rows: list, num_bins: int = 20
) -> list[HistogramBucket]:
    """Helper to bin grouped data into a fixed number of buckets."""
    if not data_rows:
        return []

    # data_rows are expected to be sorted by value
    # and have .value and .count attributes
    min_val = data_rows[0].value
    max_val = data_rows[-1].value

    span = max_val - min_val

    # If single value or no span, return single bucket
    if span == 0:
        total_count = sum(row.count for row in data_rows)
        return [
            HistogramBucket(value=int(min_val), count=total_count, label=str(min_val))
        ]

    # Calculate step size to be a whole number (integer)
    # We want roughly num_bins, so step = ceil(span / num_bins)
    step = math.ceil(span / num_bins)
    if step < 1:
        step = 1

    # Recalculate number of bins based on the integer step
    # We maintain the passed num_bins as the fixed output size if possible,
    # or effectively we cover the span.
    # The user asked for "based on a bin size of 20", so we try to output 20 buckets.
    # However, if step is large due to ceil, the last buckets might be empty.

    buckets = [0] * num_bins
    # Calculate bucket starts for the response
    bucket_starts = [int(min_val + i * step) for i in range(num_bins)]

    for row in data_rows:
        val = row.value
        # Determine bin index
        idx = int((val - min_val) / step)
        # Clamp max value to last bin (if it exceeds due to some reason, though logic says it shouldn't if num_bins covers it)
        # But wait, if step is rounded up, idx will be smaller.
        # e.g. span=30, num_bins=20, step=2. val=30. idx=15.
        # If val is very large? No, max_val is used to compute span.
        if idx >= num_bins:
            idx = num_bins - 1
        buckets[idx] += row.count

    return [
        HistogramBucket(value=v, count=c, label=f"{v}-{v + step}")
        for v, c in zip(bucket_starts, buckets)
    ]


class DropoutPoint(BaseModel):
    """Count of dropouts at a specific question."""

    main_question: int | None
    sub_question: int | None
    count: int


class MonitoringStats(BaseModel):
    """Aggregated monitoring statistics for a project."""

    # Basic counts
    total_interviews: int
    completion_rate: float

    # Breakdowns
    interviews_by_status: list[InterviewStatusCount]

    # Time series data
    interviews_over_time: list[DailyInterviewCount]
    interviews_by_time_of_day: list[InterviewTimeOfDayCount]

    # Duration/engagement stats
    duration_stats: InterviewDurationStats | None
    message_count_stats: MessageCountStats | None

    # Histogram distributions
    duration_histogram: list[HistogramBucket]
    message_count_histogram: list[HistogramBucket]
    message_length_histogram: list[HistogramBucket]

    # Dropout analysis
    dropout_stats: list[DropoutPoint]


@router.get(
    "/projects/{project_id}/stats",
    description="Get monitoring statistics for a project's interviews",
)
async def get_project_monitoring_stats(
    project_id: UUID4,
    db: DBSession,
    jwt: DemoToken,
    interview_types: Annotated[
        list[InterviewType],
        Query(default_factory=lambda: [t.value for t in InterviewType]),
    ],
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
    interview_conditions = [
        InterviewTable.project_id == project_id,
        InterviewTable.type.in_(interview_types),
    ]
    message_conditions = [
        MessageTable.project_id == project_id,
        MessageTable.interview_type.in_(interview_types),
    ]

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

    # Completion rate
    completion_rate = (
        (total_completed / total_interviews) if total_interviews > 0 else 0.0
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

    # Interviews by time of day (grouped by hour)
    hour_extract = func.extract("hour", InterviewTable.created_at)
    time_of_day_stmt = (
        select(
            hour_extract.label("hour"),
            func.count(InterviewTable.id).label("count"),
        )
        .where(*interview_conditions)
        .group_by(hour_extract)
        .order_by(hour_extract)
    )
    time_of_day_results = session.execute(time_of_day_stmt).all()
    hour_counts = {int(row.hour): row.count for row in time_of_day_results}
    interviews_by_time_of_day = [
        InterviewTimeOfDayCount(
            time=datetime.time(hour=h).strftime("%H"),
            count=hour_counts.get(h, 0),
        )
        for h in range(24)
    ]

    # ++++++++++++++++++++++++++++++ #
    # Stats for COMPLETED interviews #
    # ++++++++++++++++++++++++++++++ #

    # Duration statistics
    duration_conditions = completed_conditions + [InterviewTable.total_time_spent > 0]
    duration_stmt = select(
        func.min(InterviewTable.total_time_spent).label("min_seconds"),
        func.max(InterviewTable.total_time_spent).label("max_seconds"),
        func.avg(InterviewTable.total_time_spent).label("avg_seconds"),
        func.sum(InterviewTable.total_time_spent).label("sum_seconds"),
    ).where(*duration_conditions)
    duration_result = session.execute(duration_stmt).first()

    duration_stats = None
    if duration_result and duration_result.min_seconds is not None:
        duration_stats = InterviewDurationStats(
            min_seconds=duration_result.min_seconds,
            max_seconds=duration_result.max_seconds,
            avg_seconds=float(duration_result.avg_seconds or 0),
            sum_seconds=duration_result.sum_seconds,
        )

    # Message count statistics per interview
    message_counts_subquery = (
        select(
            MessageTable.interview_id,
            func.count(MessageTable.id).label("msg_count"),
        )
        .where(
            *message_conditions,
            MessageTable.interview_id.in_(
                select(InterviewTable.id).where(*completed_conditions)
            ),
        )
        .group_by(MessageTable.interview_id)
        .subquery()
    )

    msg_stats_stmt = select(
        func.min(message_counts_subquery.c.msg_count).label("min_messages"),
        func.max(message_counts_subquery.c.msg_count).label("max_messages"),
        func.avg(message_counts_subquery.c.msg_count).label("avg_messages"),
        func.sum(message_counts_subquery.c.msg_count).label("sum_messages"),
    )
    msg_stats_result = session.execute(msg_stats_stmt).first()

    message_count_stats = None
    if msg_stats_result and msg_stats_result.min_messages is not None:
        message_count_stats = MessageCountStats(
            min_messages=msg_stats_result.min_messages,
            max_messages=msg_stats_result.max_messages,
            avg_messages=float(msg_stats_result.avg_messages or 0),
            sum_messages=msg_stats_result.sum_messages,
        )

    # Duration histogram (one entry per distinct total_time_spent value)
    duration_hist_stmt = (
        select(
            InterviewTable.total_time_spent.label("value"),
            func.count(InterviewTable.id).label("count"),
        )
        .where(*duration_conditions)
        .group_by(InterviewTable.total_time_spent)
        .order_by(InterviewTable.total_time_spent)
    )
    duration_rows = session.execute(duration_hist_stmt).all()
    duration_histogram = _compute_histogram_buckets(duration_rows)

    # Message count histogram (one entry per distinct message count per interview)
    msg_count_hist_stmt = (
        select(
            message_counts_subquery.c.msg_count.label("value"),
            func.count().label("count"),
        )
        .group_by(message_counts_subquery.c.msg_count)
        .order_by(message_counts_subquery.c.msg_count)
    )
    msg_count_rows = session.execute(msg_count_hist_stmt).all()
    message_count_histogram = _compute_histogram_buckets(msg_count_rows)

    # Message length histogram (one entry per distinct character length)
    msg_length_stmt = (
        select(
            func.length(MessageTable.content).label("value"),
            func.count(MessageTable.id).label("count"),
        )
        .where(
            *message_conditions,
            MessageTable.interview_id.in_(
                select(InterviewTable.id).where(*completed_conditions)
            ),
        )
        .group_by(func.length(MessageTable.content))
        .order_by(func.length(MessageTable.content))
    )
    msg_length_rows = session.execute(msg_length_stmt).all()
    message_length_histogram = _compute_histogram_buckets(msg_length_rows)

    # ++++++++++++++++++++++++++++++ #
    # Stats for INCOMPLETE interviews #
    # ++++++++++++++++++++++++++++++ #

    # Filter for incomplete interviews
    incomplete_conditions = interview_conditions + [
        InterviewTable.status != InterviewStatus.COMPLETED,
    ]

    # Find the last message for each incomplete interview
    last_msg_subquery = (
        select(
            MessageTable.interview_id,
            func.max(MessageTable.message_id).label("max_msg_id"),
        )
        .where(
            MessageTable.interview_id.in_(
                select(InterviewTable.id).where(*incomplete_conditions)
            ),
        )
        .group_by(MessageTable.interview_id)
        .subquery()
    )

    # Count dropouts by question
    dropout_stmt = (
        select(
            MessageTable.main_question,
            MessageTable.sub_question,
            func.count(MessageTable.id).label("count"),
        )
        .where(
            MessageTable.interview_id.in_(
                select(InterviewTable.id).where(
                    *interview_conditions,
                    InterviewTable.status == InterviewStatus.INACTIVE,
                )
            ),
        )
        .join(
            last_msg_subquery,
            (MessageTable.interview_id == last_msg_subquery.c.interview_id)
            & (MessageTable.message_id == last_msg_subquery.c.max_msg_id),
        )
        .group_by(MessageTable.main_question, MessageTable.sub_question)
        .order_by(MessageTable.main_question, MessageTable.sub_question)
    )

    dropout_results = session.execute(dropout_stmt).all()
    dropout_stats = [
        DropoutPoint(
            main_question=row.main_question,
            sub_question=row.sub_question,
            count=row.count,
        )
        for row in dropout_results
    ]

    return MonitoringStats(
        total_interviews=total_interviews,
        completion_rate=completion_rate,
        interviews_by_status=interviews_by_status,
        interviews_over_time=interviews_over_time,
        interviews_by_time_of_day=interviews_by_time_of_day,
        duration_stats=duration_stats,
        message_count_stats=message_count_stats,
        duration_histogram=duration_histogram,
        message_count_histogram=message_count_histogram,
        message_length_histogram=message_length_histogram,
        dropout_stats=dropout_stats,
    )
