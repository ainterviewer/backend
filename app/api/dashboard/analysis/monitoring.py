import datetime
import math
from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import UUID4, BaseModel
from sqlalchemy import case, func, select

from ainterviewer.types import InterviewStatus

from ....db.tables import InterviewTable, MessageTable
from ....db.types import InterviewType
from ....dependencies import DBSession, DemoToken

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


# Mantissas that read as "round" on an axis. Only whole-number steps are used,
# so 2.5 is only ever picked from a magnitude of 10 upwards (25, 250, ...).
_NICE_MANTISSAS = (1, 2, 2.5, 5, 10)


def _nice_step(raw_step: float) -> int:
    """Round `raw_step` up to the next whole, human-readable step.

    Produces 1, 2, 5, 10, 25, 50, 100, 250, ... rather than the arbitrary
    integers a plain `ceil(span / num_bins)` yields (43, 39, ...), so axis
    labels land on values a reader can scan.
    """
    if raw_step <= 1:
        return 1

    magnitude = 10 ** math.floor(math.log10(raw_step))
    for mantissa in _NICE_MANTISSAS:
        candidate = mantissa * magnitude
        if candidate >= raw_step and float(candidate).is_integer():
            return int(candidate)

    # log10 rounding can leave us just past 10 * magnitude; the next decade is
    # always nice and always large enough.
    return int(10 * magnitude)


def _compute_histogram_buckets(
    data_rows: Sequence, num_bins: int = 20
) -> list[HistogramBucket]:
    """Bin grouped value/count rows into `num_bins` evenly spaced buckets.

    Bucket width is a "nice" number and the first bucket edge is snapped down to
    a multiple of that width, so the axis reads 300, 350, 400, ... instead of
    321, 364, 407, ...
    """
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

    # Snapping the origin down costs at most one bucket of headroom, so size the
    # step against `num_bins - 1` buckets and then verify coverage explicitly.
    step = _nice_step((span + 1) / (num_bins - 1))
    start = int(math.floor(min_val / step) * step)
    while start + num_bins * step <= max_val:
        step = _nice_step(step + 1)
        start = int(math.floor(min_val / step) * step)

    buckets = [0] * num_bins
    for row in data_rows:
        idx = int((row.value - start) // step)
        # Defensive clamp; the coverage loop above should make this unreachable.
        idx = max(0, min(idx, num_bins - 1))
        buckets[idx] += row.count

    return [
        HistogramBucket(
            value=start + i * step,
            count=count,
            label=f"{start + i * step}-{start + (i + 1) * step}",
        )
        for i, count in enumerate(buckets)
    ]


def _summarize(data_rows: Sequence) -> tuple[int, int, float, int] | None:
    """Derive min/max/avg/sum from value-count rows sorted by value.

    The histogram queries already group by value, so the summary statistics can
    be folded out of those rows instead of issuing a second aggregate query.
    """
    if not data_rows:
        return None

    total_count = sum(row.count for row in data_rows)
    total_sum = sum(row.value * row.count for row in data_rows)
    if total_count == 0:
        return None

    return (
        int(data_rows[0].value),
        int(data_rows[-1].value),
        total_sum / total_count,
        int(total_sum),
    )


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
def get_project_monitoring_stats(
    project_id: UUID4,
    db: DBSession,
    jwt: DemoToken,
    interview_types: Annotated[
        list[InterviewType],
        Query(default_factory=lambda: [InterviewType.DISTRIBUTED]),
    ],
    start_date: Annotated[datetime.datetime | None, Query()] = None,
    end_date: Annotated[datetime.datetime | None, Query()] = None,
) -> MonitoringStats:
    # NOTE: this endpoint is a plain `def` on purpose. The session is a
    # synchronous SQLAlchemy session, so running it as `async def` would block
    # the event loop for the whole duration of every query. As a sync endpoint
    # FastAPI runs it in a threadpool instead.
    session = db.session

    # Base conditions for filtering
    interview_conditions = [
        InterviewTable.project_id == project_id,
        InterviewTable.type.in_(interview_types),
    ]
    message_conditions = [MessageTable.project_id == project_id]

    if start_date:
        interview_conditions.append(InterviewTable.created_at >= start_date)
        message_conditions.append(MessageTable.created_at >= start_date)

    if end_date:
        interview_conditions.append(InterviewTable.created_at <= end_date)
        message_conditions.append(MessageTable.created_at <= end_date)

    # Every statistic below is derived from this one filtered set of interviews.
    # Message queries join against it rather than re-filtering through
    # `MessageTable.interview_type`, which is a hybrid property that expands to
    # a correlated subquery evaluated once per message row.
    interviews = (
        select(
            InterviewTable.id.label("id"),
            InterviewTable.status.label("status"),
            InterviewTable.created_at.label("created_at"),
            InterviewTable.total_time_spent.label("total_time_spent"),
        )
        .where(*interview_conditions)
        .cte("filtered_interviews")
    )

    def interview_ids(status: InterviewStatus):
        return select(interviews.c.id).where(interviews.c.status == status).subquery()

    completed_interviews = interview_ids(InterviewStatus.COMPLETED)
    inactive_interviews = interview_ids(InterviewStatus.INACTIVE)

    # Interviews by status. Total and completed counts are folded out of the
    # same rows instead of being counted by two extra queries.
    status_stmt = select(interviews.c.status, func.count().label("count")).group_by(
        interviews.c.status
    )
    status_results = session.execute(status_stmt).all()
    interviews_by_status = [
        InterviewStatusCount(status=status, count=count)
        for status, count in status_results
    ]

    total_interviews = sum(item.count for item in interviews_by_status)
    total_completed = next(
        (
            item.count
            for item in interviews_by_status
            if item.status == InterviewStatus.COMPLETED
        ),
        0,
    )

    # Completion rate
    completion_rate = (
        (total_completed / total_interviews) if total_interviews > 0 else 0.0
    )

    # Daily interview counts (last 30 days by default, or within date range)
    date_trunc = func.date(interviews.c.created_at)
    daily_stmt = (
        select(
            date_trunc.label("date"),
            func.count().label("count"),
            func.sum(
                case(
                    (interviews.c.status == InterviewStatus.COMPLETED, 1),
                    else_=0,
                )
            ).label("completed_count"),
        )
        .group_by(date_trunc)
        .order_by(date_trunc)
    )
    daily_results = session.execute(daily_stmt).all()
    interviews_over_time = [
        DailyInterviewCount(
            date=row.date,
            count=row.count,  # ty:ignore[invalid-argument-type]
            completed_count=row.completed_count or 0,
        )
        for row in daily_results
    ]

    # Interviews by time of day (grouped by hour)
    hour_extract = func.extract("hour", interviews.c.created_at)
    time_of_day_stmt = (
        select(
            hour_extract.label("hour"),
            func.count().label("count"),
        )
        .group_by(hour_extract)
        .order_by(hour_extract)
    )
    time_of_day_results = session.execute(time_of_day_stmt).all()
    hour_counts = {int(row.hour): row.count for row in time_of_day_results}
    interviews_by_time_of_day = [
        InterviewTimeOfDayCount(
            time=datetime.time(hour=h).strftime("%H"),
            count=hour_counts.get(h, 0),  # ty:ignore[invalid-argument-type]
        )
        for h in range(24)
    ]

    # ++++++++++++++++++++++++++++++ #
    # Stats for COMPLETED interviews #
    # ++++++++++++++++++++++++++++++ #

    # Duration histogram (one entry per distinct total_time_spent value).
    # min/max/avg/sum are derived from these rows.
    duration_hist_stmt = (
        select(
            interviews.c.total_time_spent.label("value"),
            func.count().label("count"),
        )
        .where(
            interviews.c.status == InterviewStatus.COMPLETED,
            interviews.c.total_time_spent > 0,
        )
        .group_by(interviews.c.total_time_spent)
        .order_by(interviews.c.total_time_spent)
    )
    duration_rows = session.execute(duration_hist_stmt).all()
    duration_histogram = _compute_histogram_buckets(duration_rows)

    duration_summary = _summarize(duration_rows)
    duration_stats = (
        InterviewDurationStats(
            min_seconds=duration_summary[0],
            max_seconds=duration_summary[1],
            avg_seconds=duration_summary[2],
            sum_seconds=duration_summary[3],
        )
        if duration_summary
        else None
    )

    # Message counts per completed interview
    message_counts_subquery = (
        select(
            MessageTable.interview_id,
            func.count().label("msg_count"),
        )
        .select_from(MessageTable)
        .join(
            completed_interviews,
            MessageTable.interview_id == completed_interviews.c.id,
        )
        .where(*message_conditions)
        .group_by(MessageTable.interview_id)
        .subquery()
    )

    # Message count histogram (one entry per distinct message count per
    # interview). min/max/avg/sum are derived from these rows.
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

    msg_summary = _summarize(msg_count_rows)
    message_count_stats = (
        MessageCountStats(
            min_messages=msg_summary[0],
            max_messages=msg_summary[1],
            avg_messages=msg_summary[2],
            sum_messages=msg_summary[3],
        )
        if msg_summary
        else None
    )

    # Message length histogram (one entry per distinct character length)
    msg_length = func.length(MessageTable.content)
    msg_length_stmt = (
        select(
            msg_length.label("value"),
            func.count().label("count"),
        )
        .select_from(MessageTable)
        .join(
            completed_interviews,
            MessageTable.interview_id == completed_interviews.c.id,
        )
        .where(*message_conditions)
        .group_by(msg_length)
        .order_by(msg_length)
    )
    msg_length_rows = session.execute(msg_length_stmt).all()
    message_length_histogram = _compute_histogram_buckets(msg_length_rows)

    # +++++++++++++++++++++++++++++++ #
    # Stats for INACTIVE interviews   #
    # +++++++++++++++++++++++++++++++ #

    # Find the last message of each inactive interview
    last_msg_subquery = (
        select(
            MessageTable.interview_id,
            func.max(MessageTable.message_id).label("max_msg_id"),
        )
        .select_from(MessageTable)
        .join(
            inactive_interviews,
            MessageTable.interview_id == inactive_interviews.c.id,
        )
        .group_by(MessageTable.interview_id)
        .subquery()
    )

    # Count dropouts by question
    dropout_stmt = (
        select(
            MessageTable.main_question,
            MessageTable.sub_question,
            func.count().label("count"),
        )
        .select_from(MessageTable)
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
            count=row.count,  # ty:ignore[invalid-argument-type]
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
