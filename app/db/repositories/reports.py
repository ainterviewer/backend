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
from typing import Literal, TypeVar

from pydantic import UUID4
from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.orm import aliased

from ainterviewer.utils import now

from ..models import (
    MessageReportAdminPublic,
    MessageReportAdminRowPublic,
    MessageReportRowPublic,
)
from ..tables import (
    CollaboratorTable,
    InterviewTable,
    MessageReportReadTable,
    MessageReportTable,
    MessageTable,
    ParticipantTable,
    ProjectFolderTable,
    ProjectParticipantTable,
    ProjectTable,
)
from ..types import ReportStatus
from .base import BaseRepository

logger = logging.getLogger(__name__)

#: Which of the two review tracks a listing filters on, or a resolve writes to.
ReviewTrack = Literal["owner", "admin"]

#: The row model a listing builds. Parameterising on it is what keeps the
#: platform's review track out of the project queue: the narrow model has no
#: field for it, so a caller cannot serialize it by accident.
RowT = TypeVar("RowT", bound=MessageReportRowPublic)


class ReportRepository(BaseRepository):
    """Repository for reviewing message reports."""

    @staticmethod
    def _collaborated_projects(user_id: UUID4):
        """The projects this user has any role on, as a subquery.

        Reached through the folder, exactly as
        `ProjectRepository.get_user_role_on_project` does -- a project's owner
        holds a collaborator row on its folder, so owners are included.
        """
        return (
            select(ProjectTable.id)
            .join(
                ProjectFolderTable,
                ProjectTable.folder_id == ProjectFolderTable.id,
            )
            .join(
                CollaboratorTable,
                CollaboratorTable.folder_id == ProjectFolderTable.id,
            )
            .where(CollaboratorTable.user_id == user_id)
        )

    @staticmethod
    def _status_column(track: ReviewTrack):
        return (
            MessageReportTable.status
            if track == "owner"
            else MessageReportTable.admin_status
        )

    def _list_reports(
        self,
        row_class: type[RowT],
        user_id: UUID4,
        project_id: UUID4 | None = None,
        track: ReviewTrack = "owner",
        statuses: list[ReportStatus] | None = None,
        unread_only: bool = False,
        collaborated_only: bool = False,
    ) -> list[RowT]:
        """Every report a reviewer may see, newest first.

        Called through `list_reports` or `list_admin_reports`, which fix
        `row_class` and the track together -- the two cannot be mixed up, so
        the project queue has no way to come back carrying the platform's
        review.

        Scope comes from one of two places, and a caller outside the admin
        queue must pass one of them:

        * `project_id` -- one project, for the project endpoint, which has
          already role-checked the id in its own path.
        * `collaborated_only` -- every project the user has a role on, for
          the cross-project inbox, which has no path to check.

        Neither is optional in the sense of "convenient": with both left off
        this returns every report on the platform, which is right for the
        admin queue and a leak anywhere else. `collaborated_only` is
        deliberately not implied by `track="owner"`, because a platform admin
        may read one project's owner-track queue without collaborating on it
        -- the role checker lets them through, and scoping by collaboration
        would hand them an empty page instead.

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

        if collaborated_only:
            statement = statement.where(
                MessageReportTable.project_id.in_(self._collaborated_projects(user_id))
            )

        if statuses:
            statement = statement.where(self._status_column(track).in_(statuses))

        if unread_only:
            statement = statement.where(read.id.is_(None))

        return [
            row_class(
                **MessageReportAdminPublic.model_validate(report).model_dump(
                    include=set(row_class.model_fields)
                ),
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

    def list_reports(
        self,
        user_id: UUID4,
        project_id: UUID4 | None = None,
        statuses: list[ReportStatus] | None = None,
        unread_only: bool = False,
        collaborated_only: bool = False,
    ) -> list[MessageReportRowPublic]:
        """The project-side queue: one project's reports, or the caller's own.

        Always the project track, and always the narrow row model -- see
        `MessageReportPublic` for why the platform's review is not in it.
        """
        return self._list_reports(
            MessageReportRowPublic,
            user_id=user_id,
            project_id=project_id,
            track="owner",
            statuses=statuses,
            unread_only=unread_only,
            collaborated_only=collaborated_only,
        )

    def list_admin_reports(
        self,
        user_id: UUID4,
        statuses: list[ReportStatus] | None = None,
        unread_only: bool = False,
    ) -> list[MessageReportAdminRowPublic]:
        """The platform queue: every report there is, with the admin track.

        Takes no project and no collaboration scope: this is the whole
        platform, which is the point of it.
        """
        return self._list_reports(
            MessageReportAdminRowPublic,
            user_id=user_id,
            track="admin",
            statuses=statuses,
            unread_only=unread_only,
        )

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

    def unread_count(self, user_id: UUID4, track: ReviewTrack) -> int:
        """How many still-open reports this reviewer has not read.

        Both halves matter: a resolved report is off the queue whether or not
        anyone ticked it as read, and a report that has been read is not
        thereby resolved. The status is the named track's, so an owner
        rewording a question does not empty the platform's badge.

        On the `owner` track the count is confined to projects the user
        collaborates on, reached through the folder exactly as
        `ProjectRepository.get_user_role_on_project` does -- a project's owner
        holds a collaborator row on its folder, so owners are included. The
        `admin` track spans every project, which is what the platform queue
        is.
        """
        unread = ~(
            select(MessageReportReadTable.id)
            .where(
                MessageReportReadTable.report_id == MessageReportTable.id,
                MessageReportReadTable.user_id == user_id,
            )
            .exists()
        )

        conditions = [self._status_column(track) == ReportStatus.OPEN, unread]

        if track == "owner":
            conditions.append(
                MessageReportTable.project_id.in_(self._collaborated_projects(user_id))
            )

        return self.session.execute(
            select(func.count(MessageReportTable.id)).where(*conditions)
        ).scalar_one()
