"""The set of interviews an analysis page counts over.

Shared by monitoring and the report, which ask the same question of the same
table and must answer it identically: a respondent who restarted three times is
one respondent on both pages or the two pages disagree about the project.
"""

from collections.abc import Sequence

from sqlalchemy import CTE, case, func, or_, select
from sqlalchemy.sql import ColumnElement

from ainterviewer.types import InterviewStatus

from ....db.tables import InterviewTable, ParticipantTable, ProjectParticipantTable


def interview_cohort(
    columns: Sequence[ColumnElement],
    conditions: Sequence[ColumnElement[bool]],
    *,
    deduplicate_by_pid: bool = False,
    name: str = "filtered_interviews",
) -> CTE:
    """The filtered interviews, optionally one per participant.

    `columns` are the interview columns the caller needs, each `.label()`-ed;
    the CTE carries exactly those, under exactly those names, whether or not
    deduplication is on -- so the query built on top of it does not have to
    know which branch produced it.

    With `deduplicate_by_pid`, one row survives per participant `pid`: the
    interview that got furthest. COMPLETED beats ACTIVE beats INACTIVE, and
    only within the same status does recency decide. `last_updated` is only
    written on update, so it falls back to `created_at`; `id` makes the pick
    stable when even those tie.
    """
    if not deduplicate_by_pid:
        return select(*columns).where(*conditions).cte(name)

    status_rank = case(
        (InterviewTable.status == InterviewStatus.COMPLETED, 2),
        (InterviewTable.status == InterviewStatus.ACTIVE, 1),
        else_=0,
    )
    ranked = (
        select(
            *columns,
            ParticipantTable.pid.label("pid"),
            func.row_number()
            .over(
                partition_by=ParticipantTable.pid,
                order_by=(
                    status_rank.desc(),
                    func.coalesce(
                        InterviewTable.last_updated, InterviewTable.created_at
                    ).desc(),
                    InterviewTable.id.desc(),
                ),
            )
            .label("rank"),
        )
        .select_from(InterviewTable)
        # Outer joins: `pid` hangs two hops off the interview and every hop is
        # optional (never distributed to a participant, participant since
        # deleted, participant with no `pid` set). Those interviews have to
        # survive the join to survive the filter below.
        .outerjoin(
            ProjectParticipantTable,
            InterviewTable.participant_id == ProjectParticipantTable.id,
        )
        .outerjoin(
            ParticipantTable,
            ProjectParticipantTable.participant_id == ParticipantTable.id,
        )
        .where(*conditions)
        .subquery()
    )

    return (
        select(*(ranked.c[column.name] for column in columns))
        # A NULL `pid` is not a duplicate key: SQL puts every such row in one
        # partition, so ranking alone would collapse all pid-less interviews
        # into a single one. They are let through unranked.
        .where(or_(ranked.c.rank == 1, ranked.c.pid.is_(None)))
        .cte(name)
    )
