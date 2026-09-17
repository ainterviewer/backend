"""Reading and resolving the reports respondents file against questions.

The write side lives in `InterviewRepository.create_message_report`: that is
the respondent's, made under an interview token. This is the reviewers' side,
and there are two of them. A project member reviews the reports on their own
project; a platform admin reviews every report there is. Each has its own
status on the row -- see `MessageReportTable` -- so neither can clear the
other's queue, and the two endpoints that call in here differ only in whether
they pass a `project_id` and which track they name.
"""

import logging
from collections.abc import Sequence
from typing import Literal

from pydantic import UUID4
from sqlalchemy import and_, insert, select, update
from sqlalchemy.orm import aliased

from ainterviewer.utils import now

from ..models import MessageReportPublic, MessageReportRowPublic
from ..tables import (
    InterviewTable,
    MessageReportReadTable,
    MessageReportTable,
    MessageTable,
    ParticipantTable,
    ProjectParticipantTable,
    ProjectTable,
)
from ..types import ReportStatus
from .base import BaseRepository

logger = logging.getLogger(__name__)

#: Which of the two review tracks a listing filters on, or a resolve writes to.
ReviewTrack = Literal["owner", "admin"]


class ReportRepository(BaseRepository):
    """Repository for reviewing message reports."""

    @staticmethod
    def _status_column(track: ReviewTrack):
        return (
            MessageReportTable.status
            if track == "owner"
            else MessageReportTable.admin_status
        )

    def list_reports(
        self,
        user_id: UUID4,
        project_id: UUID4 | None = None,
        track: ReviewTrack = "owner",
        statuses: list[ReportStatus] | None = None,
        unread_only: bool = False,
    ) -> list[MessageReportRowPublic]:
        """Every report a reviewer may see, newest first.

        `project_id` is what separates the two callers: the project endpoint
        passes it and sees one project, the admin endpoint omits it and sees
        all of them. It is not optional in the sense of "convenient" -- an
        endpoint that forgets it shows one project's members another's
        reports, so the project router passes it from the path it already
        role-checks.

        The question itself is joined in rather than left to the client. A
        report names a message by id, and a queue of ids is not something
        anyone can review: what is being judged is the wording of the
        question, so the wording has to be in the row.

        `read_by_me` is per-caller by construction -- an outer join against
        this user's read rows -- so two reviewers never see each other's read
        state as their own. Not paginated: reports are rare, and both queues
        are read whole so that the client can count and facet them.
        """
        read = aliased(MessageReportReadTable)

        statement = (
            select(
                MessageReportTable,
                MessageTable.message_id.label("question_number"),
                MessageTable.content.label("question"),
                ProjectTable.title.label("project_title"),
                InterviewTable.language.label("language"),
                ParticipantTable.pid.label("pid"),
                read.id.is_not(None).label("read_by_me"),
            )
            .join(MessageTable, MessageReportTable.message_id == MessageTable.id)
            .join(ProjectTable, MessageReportTable.project_id == ProjectTable.id)
            .join(InterviewTable, MessageReportTable.interview_id == InterviewTable.id)
            # The participant is who the interview was distributed to, and an
            # interview often has none at all -- hence outer joins the whole
            # way down. Mirrors `InterviewRepository._join_filter_sources`.
            .outerjoin(
                ProjectParticipantTable,
                InterviewTable.participant_id == ProjectParticipantTable.id,
            )
            .outerjoin(
                ParticipantTable,
                ProjectParticipantTable.participant_id == ParticipantTable.id,
            )
            .outerjoin(
                read,
                and_(
                    read.report_id == MessageReportTable.id,
                    read.user_id == user_id,
                ),
            )
            .order_by(MessageReportTable.created_at.desc())
        )

        if project_id is not None:
            statement = statement.where(MessageReportTable.project_id == project_id)

        if statuses:
            statement = statement.where(self._status_column(track).in_(statuses))

        if unread_only:
            statement = statement.where(read.id.is_(None))

        return [
            MessageReportRowPublic(
                **MessageReportPublic.model_validate(report).model_dump(),
                question_number=question_number,
                question=question,
                project_title=project_title,
                language=language,
                pid=pid,
                read_by_me=read_by_me,
            )
            for (
                report,
                question_number,
                question,
                project_title,
                language,
                pid,
                read_by_me,
            ) in self.session.execute(statement).all()
        ]

    def mark_read(
        self,
        user_id: UUID4,
        report_ids: Sequence[UUID4],
        project_id: UUID4 | None = None,
    ) -> int:
        """Record that this reviewer has seen these reports.

        Idempotent: already-read rows are skipped rather than re-stamped, so
        `read_at` stays the moment the reviewer first saw the report and a
        client that marks a page read on every visit does not keep moving it.

        Returns the number of rows actually inserted, which is the number of
        reports that were new to this reviewer.
        """
        if not report_ids:
            return 0

        visible = self._visible_ids(report_ids, project_id)
        if not visible:
            return 0

        already = set(
            self.session.execute(
                select(MessageReportReadTable.report_id).where(
                    MessageReportReadTable.report_id.in_(visible),
                    MessageReportReadTable.user_id == user_id,
                )
            )
            .scalars()
            .all()
        )

        fresh = [report_id for report_id in visible if report_id not in already]
        if not fresh:
            return 0

        self.session.execute(
            insert(MessageReportReadTable),
            [
                {"report_id": report_id, "user_id": user_id, "read_at": now()}
                for report_id in fresh
            ],
        )
        self.session.commit()
        return len(fresh)

    def resolve(
        self,
        user_id: UUID4,
        report_ids: Sequence[UUID4],
        status: ReportStatus,
        track: ReviewTrack,
        project_id: UUID4 | None = None,
    ) -> int:
        """Set one track's status on these reports. Returns the rows changed.

        Writes only the named track's three columns, so a project member
        resolving a report leaves the admin queue exactly as it was, and vice
        versa. Reopening (`status=OPEN`) clears the resolver and timestamp
        rather than leaving them behind: a report that is open again was not
        resolved by anybody.
        """
        if not report_ids:
            return 0

        visible = self._visible_ids(report_ids, project_id)
        if not visible:
            return 0

        resolved = status is not ReportStatus.OPEN
        stamp = now() if resolved else None
        resolver = user_id if resolved else None

        values = (
            {"status": status, "resolved_by_id": resolver, "resolved_at": stamp}
            if track == "owner"
            else {
                "admin_status": status,
                "admin_resolved_by_id": resolver,
                "admin_resolved_at": stamp,
            }
        )

        result = self.session.execute(
            update(MessageReportTable)
            .where(MessageReportTable.id.in_(visible))
            .values(**values)
        )
        self.session.commit()
        return result.rowcount or 0  # ty: ignore[unresolved-attribute]

    def _visible_ids(
        self, report_ids: Sequence[UUID4], project_id: UUID4 | None
    ) -> list[UUID4]:
        """Those of `report_ids` the caller is allowed to act on.

        A project member's calls are scoped to the project the endpoint
        role-checked, so an id belonging to another project is dropped here
        rather than acted on -- the ids arrive in the request body, and the
        role check proves membership of the project in the path and nothing
        about them. An admin passes no project and reaches all of them.
        """
        statement = select(MessageReportTable.id).where(
            MessageReportTable.id.in_(report_ids)
        )
        if project_id is not None:
            statement = statement.where(MessageReportTable.project_id == project_id)
        return list(self.session.execute(statement).scalars().all())
