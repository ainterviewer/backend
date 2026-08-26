import datetime
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import UUID4
from sqlalchemy import String, case, cast, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload, noload, selectinload
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.sql.elements import ColumnElement

from ainterviewer.interview_guides import Image, InterviewGuide, SurveyItem
from ainterviewer.types import (
    Feedback,
    Interviewer,
    InterviewStatus,
    MessageRole,
    MessageType,
)
from ainterviewer.utils import now

from ...utils import as_aware
from ..models import (
    IntervieweeCreate,
    IntervieweePublic,
    InterviewPublic,
    InterviewSummaryPublic,
    MessagePublic,
)
from ..tables import (
    IntervieweeTable,
    InterviewResumeTokenTable,
    InterviewTable,
    MessageAnnotationTable,
    MessageTable,
    ParticipantTable,
    PlatformReleaseTable,
    ProjectParticipantTable,
    ProjectTable,
    TaskTable,
    TestRunTable,
    TestSetupTable,
)
from ..types import InterviewType
from .base import BaseRepository
from .errors import ResumeTokenError

logger = logging.getLogger(__name__)


SORTABLE_INTERVIEW_COLUMNS = frozenset(
    {
        "created_at",
        "last_updated",
        "status",
        "language",
        "type",
        "n_messages",
        "test_name",
        "pid",
    }
)
"""Columns `get_interviews` will sort by.

The list API validates against this before calling, so a header the client
should not have offered is rejected as a bad request rather than raised as a
ValueError from the repository.
"""


class InterviewRepository(BaseRepository):
    """Repository for Interview, Message, and Task operations."""

    # ==================== Interview Methods ====================
    def change_active_to_inactive(self):
        self.session.execute(
            update(InterviewTable)
            .where(InterviewTable.status == InterviewStatus.ACTIVE)
            .values(status=InterviewStatus.INACTIVE)
        )
        self.session.commit()

    def create_interview(
        self,
        project_id: UUID4,
        interview_guide: InterviewGuide,
        interview_type: InterviewType,
        interviewer: Interviewer = Interviewer.AI,
        **kwargs,
    ) -> InterviewPublic:
        platform_version = self.session.execute(
            select(PlatformReleaseTable.platform_release_version)
            .order_by(PlatformReleaseTable.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

        interview = InterviewTable(
            project_id=project_id,
            interview_guide=interview_guide,
            type=interview_type,
            interviewer=interviewer,
            platform_version=platform_version,
            **kwargs,
        )

        self.session.add(interview)
        self.session.commit()
        self.session.refresh(interview)
        # The interview was just created, so its transcript is empty by
        # construction. Mark the collection loaded rather than letting
        # InterviewPublic's `messages` field trigger a query for it.
        set_committed_value(interview, "messages", [])

        return InterviewPublic.model_validate(interview)

    def update_interview_guide(
        self,
        project_id: UUID4,
        interview_id: UUID4,
        interview_guide: InterviewGuide,
    ):
        statement = (
            update(InterviewTable)
            .where(
                InterviewTable.project_id == project_id,
                InterviewTable.id == interview_id,
            )
            .values(interview_guide=interview_guide)
        )

        self.session.execute(statement)
        self.session.commit()

    def delete_interviews(
        self,
        project_id: UUID4,
        interview_ids: list[UUID4],
    ):
        statement = select(ProjectTable).where(
            ProjectTable.id == project_id,
        )
        self.session.execute(statement).scalar_one()

        # Children first, then the interviews, in a single transaction. All
        # three child tables declare ON DELETE CASCADE, but SQLite only
        # enforces foreign keys on connections that ran `PRAGMA
        # foreign_keys=ON` and the app does not currently enable it, so the
        # cascade cannot be relied on -- leaving these out is what orphaned the
        # task and interviewee rows already in the database. This order is
        # correct whether or not the cascade fires.
        for table in (MessageTable, TaskTable, IntervieweeTable):
            self.session.execute(
                delete(table).where(
                    table.project_id == project_id,
                    table.interview_id.in_(interview_ids),
                )
            )
        self.session.execute(
            delete(InterviewTable).where(
                InterviewTable.project_id == project_id,
                InterviewTable.id.in_(interview_ids),
            )
        )
        self.session.commit()

    @staticmethod
    def _test_name_column():
        """Mirrors InterviewTable.test_name: only synthetic test runs carry one."""
        return case(
            (
                InterviewTable.type == InterviewType.SYNTHETIC_TEST,
                TestSetupTable.name,
            ),
            else_=None,
        ).label("test_name")

    @staticmethod
    def _join_filter_sources(statement):
        """Join everything a filter, sort or selected column reaches through.

        test_name comes from the test run and pid from the participant, and
        both are searchable. Every statement built from `_interview_filters`
        needs these joins, not just the one selecting rows: a search on the
        test name or pid is a condition like any other, and the count and
        facet queries apply it too. All outer joins -- an interview has a test
        run or a participant, never both, and often neither.
        """
        return (
            statement.outerjoin(
                TestRunTable, InterviewTable.test_run_id == TestRunTable.id
            )
            .outerjoin(TestSetupTable, TestRunTable.test_setup_id == TestSetupTable.id)
            .outerjoin(
                ProjectParticipantTable,
                InterviewTable.participant_id == ProjectParticipantTable.id,
            )
            .outerjoin(
                ParticipantTable,
                ProjectParticipantTable.participant_id == ParticipantTable.id,
            )
        )

    @staticmethod
    def _interview_filters(
        project_id: UUID4,
        interview_types: list[InterviewType] | None = None,
        selected_types: list[InterviewType] | None = None,
        statuses: list[InterviewStatus] | None = None,
        languages: list[str] | None = None,
        created_from: datetime.datetime | None = None,
        created_to: datetime.datetime | None = None,
        completed: bool | None = None,
        search: str | None = None,
        pid: str | None = None,
    ) -> dict[str, ColumnElement[bool]]:
        """The active filters, keyed by the facet each one belongs to.

        Keyed rather than listed because a facet's own selection must be left
        out when counting that facet: with it applied, every unselected value
        would report zero and the dropdown could never be widened.

        `interview_types` and `selected_types` both constrain the type column
        but are not interchangeable. The first is the scope of the list -- the
        interviews page shows distributed interviews, the test results page
        shows test runs -- and holds for the facet counts too, or the results
        page would offer a `distributed` option that its own list can never
        contain. The second is what the user picked inside that scope, and is
        dropped when counting the type facet like any other selection.
        """
        filters: dict[str, ColumnElement[bool]] = {
            "project": InterviewTable.project_id == project_id,
        }

        if interview_types:
            filters["scope"] = InterviewTable.type.in_(interview_types)

        if selected_types:
            filters["type"] = InterviewTable.type.in_(selected_types)

        if statuses:
            filters["status"] = InterviewTable.status.in_(statuses)

        if languages:
            # Stored upper-cased by LanguageType; normalise so a lower-case
            # query param still matches.
            filters["language"] = InterviewTable.language.in_(
                [language.upper() for language in languages]
            )

        if created_from is not None:
            filters["created_from"] = InterviewTable.created_at >= created_from

        if created_to is not None:
            filters["created_to"] = InterviewTable.created_at <= created_to

        if completed is not None:
            filters["completed"] = (
                InterviewTable.status == InterviewStatus.COMPLETED
                if completed
                else InterviewTable.status != InterviewStatus.COMPLETED
            )

        if pid:
            # An exact match, not a search: this is the "show me this
            # participant's interviews" link from the participants table.
            filters["pid"] = ParticipantTable.pid == pid

        if search and (term := search.strip()):
            pattern = f"%{term}%"
            filters["search"] = or_(
                # Ids are what the list actually shows, so they are what a
                # search has to match; cast because the column is a UUID on
                # Postgres. ilike() degrades to lower(x) LIKE lower(y) on
                # SQLite, which has no ILIKE.
                cast(InterviewTable.id, String).ilike(pattern),
                TestSetupTable.name.ilike(pattern),
                ParticipantTable.pid.ilike(pattern),
            )

        return filters

    def get_interviews(
        self,
        project_id: UUID4,
        offset: int | None = None,
        limit: int | None = None,
        sorting_column: str = "created_at",
        sorting_order: Literal["desc", "asc"] = "desc",
        interview_types: list[InterviewType] | None = None,
        selected_types: list[InterviewType] | None = None,
        statuses: list[InterviewStatus] | None = None,
        languages: list[str] | None = None,
        created_from: datetime.datetime | None = None,
        created_to: datetime.datetime | None = None,
        completed: bool | None = None,
        search: str | None = None,
        pid: str | None = None,
    ) -> tuple[Sequence[InterviewSummaryPublic], int]:
        """One page of interview summaries, plus the total matching count.

        Selects columns rather than ORM objects on purpose. Returning
        InterviewTable instances would make Pydantic read every field the
        model declares, and `messages` alone pulled the full transcript of
        every interview on the page (plus a query per message for its
        annotations). `n_messages` and `test_name` are computed in SQL here
        instead of by walking relationships.
        """
        if sorting_column not in SORTABLE_INTERVIEW_COLUMNS:
            raise ValueError(f"Invalid sort column: {sorting_column}")

        test_name = self._test_name_column()
        pid_column = ParticipantTable.pid.label("pid")

        if sorting_column == "last_updated":
            # Interviews written before last_updated was maintained still have
            # NULL, and NULL ordering differs between SQLite and Postgres.
            # Matches ProjectParticipantTable.latest_interview_at.
            _sorting_col = func.coalesce(
                InterviewTable.last_updated, InterviewTable.created_at
            )
        elif sorting_column == "pid":
            # Lives on the joined participant, not on InterviewTable.
            _sorting_col = ParticipantTable.pid
        elif sorting_column == "test_name":
            # Not a column on the table: sort by the same expression the row
            # is built from rather than by the output label, which only
            # Postgres would resolve.
            _sorting_col = test_name
        else:
            _sorting_col = getattr(InterviewTable, sorting_column)

        conditions = list(
            self._interview_filters(
                project_id,
                interview_types=interview_types,
                selected_types=selected_types,
                statuses=statuses,
                languages=languages,
                created_from=created_from,
                created_to=created_to,
                completed=completed,
                search=search,
                pid=pid,
            ).values()
        )

        statement = (
            self._join_filter_sources(
                select(
                    InterviewTable.id,
                    InterviewTable.language,
                    InterviewTable.interviewer,
                    InterviewTable.status,
                    InterviewTable.type,
                    InterviewTable.created_at,
                    InterviewTable.last_updated,
                    InterviewTable.total_time_spent,
                    InterviewTable.n_messages.label("n_messages"),
                    test_name,
                    pid_column,
                )
            )
            .where(*conditions)
            .order_by(
                _sorting_col.desc() if sorting_order == "desc" else _sorting_col.asc()
            )
            .offset(offset)
            .limit(limit)
        )

        count_statement = self._join_filter_sources(
            select(func.count()).select_from(InterviewTable)
        ).where(*conditions)

        total = self.session.execute(count_statement).scalar_one()
        rows = self.session.execute(statement).all()

        return [
            InterviewSummaryPublic.model_validate(row._mapping) for row in rows
        ], total

    # ================= Interview resume links =================

    def get_resume_target(
        self, project_id: UUID4, interview_id: UUID4
    ) -> InterviewTable:
        """The interview row a resume link would unlock, scoped to its project.

        Returns the ORM row rather than InterviewPublic because the caller has
        to reach the linked participant to check whether they have opted out,
        which the public model does not carry. Raises NoResultFound when the
        interview does not exist in this project.
        """
        return self.session.execute(
            select(InterviewTable)
            .options(
                noload(InterviewTable.messages),
                joinedload(InterviewTable.project_participant).joinedload(
                    ProjectParticipantTable.participant
                ),
            )
            .where(
                InterviewTable.id == interview_id,
                InterviewTable.project_id == project_id,
            )
        ).scalar_one()

    def get_resume_token(self, interview_id: UUID4) -> InterviewResumeTokenTable | None:
        """The most recently issued link for an interview, whatever its state.

        Returns revoked and redeemed rows too: the dashboard has to be able to
        say "already used" rather than silently offering to mint another.
        """
        return self.session.execute(
            select(InterviewResumeTokenTable)
            .where(InterviewResumeTokenTable.interview_id == interview_id)
            .order_by(InterviewResumeTokenTable.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    def create_resume_token(
        self,
        interview_id: UUID4,
        token_hash: str,
        expires_at: datetime.datetime,
        created_by: UUID4 | None,
    ) -> InterviewResumeTokenTable:
        """Issue a link, revoking any outstanding one for the same interview.

        Re-issuing has to invalidate the previous link, or "regenerate" would
        quietly widen access instead of replacing it.
        """
        self.revoke_resume_tokens(interview_id)

        token = InterviewResumeTokenTable(
            interview_id=interview_id,
            token_hash=token_hash,
            expires_at=expires_at,
            created_by=created_by,
        )
        self.session.add(token)
        self.session.commit()
        self.session.refresh(token)
        return token

    def revoke_resume_tokens(self, interview_id: UUID4) -> int:
        """Revoke every still-redeemable link for an interview. Returns the count.

        Redeemed rows are left alone: they are spent already, and stamping them
        as revoked would lose the record of when they were used.
        """
        result = self.session.execute(
            update(InterviewResumeTokenTable)
            .where(
                InterviewResumeTokenTable.interview_id == interview_id,
                InterviewResumeTokenTable.revoked_at.is_(None),
                InterviewResumeTokenTable.redeemed_at.is_(None),
            )
            .values(revoked_at=now())
        )
        self.session.commit()
        return result.rowcount or 0  # ty: ignore[unresolved-attribute]

    def redeem_resume_token(self, token_hash: str) -> InterviewTable:
        """Spend a resume link and return the interview it unlocks.

        Every rejection raises ResumeTokenError, which the API renders as one
        opaque 404 -- see that class. The row is stamped inside the same
        transaction as the lookup, so two concurrent redemptions cannot both
        succeed.
        """
        token = self.session.execute(
            select(InterviewResumeTokenTable).where(
                InterviewResumeTokenTable.token_hash == token_hash
            )
        ).scalar_one_or_none()

        if token is None:
            raise ResumeTokenError("no such resume token")
        if token.revoked_at is not None:
            raise ResumeTokenError(f"resume token {token.id} was revoked")
        if token.redeemed_at is not None:
            raise ResumeTokenError(
                f"resume token {token.id} was already redeemed at {token.redeemed_at}"
            )
        # Naive coming back out of the database; see app.utils.as_aware.
        if as_aware(token.expires_at) <= now():
            raise ResumeTokenError(
                f"resume token {token.id} expired at {token.expires_at}"
            )

        interview = self.session.get(InterviewTable, token.interview_id)
        if interview is None:
            raise ResumeTokenError(f"interview {token.interview_id} no longer exists")
        if interview.status == InterviewStatus.COMPLETED:
            raise ResumeTokenError(f"interview {interview.id} is already completed")

        token.redeemed_at = now()
        self.session.commit()
        self.session.refresh(interview)
        return interview

    def get_interview_facets(
        self,
        project_id: UUID4,
        interview_types: list[InterviewType] | None = None,
        selected_types: list[InterviewType] | None = None,
        statuses: list[InterviewStatus] | None = None,
        languages: list[str] | None = None,
        created_from: datetime.datetime | None = None,
        created_to: datetime.datetime | None = None,
        completed: bool | None = None,
        search: str | None = None,
        pid: str | None = None,
    ) -> dict[str, dict[str, int]]:
        """Distinct values and their counts for each filterable column.

        The client cannot derive these: it only ever holds one page. Each
        facet is counted with every *other* filter applied, so the numbers
        answer "how many would I get if I also picked this one".
        """
        filters = self._interview_filters(
            project_id,
            interview_types=interview_types,
            selected_types=selected_types,
            statuses=statuses,
            languages=languages,
            created_from=created_from,
            created_to=created_to,
            completed=completed,
            search=search,
            pid=pid,
        )

        columns = {
            "status": InterviewTable.status,
            "language": InterviewTable.language,
            "type": InterviewTable.type,
        }

        facets: dict[str, dict[str, int]] = {}
        for facet, column in columns.items():
            conditions = [
                condition for key, condition in filters.items() if key != facet
            ]
            statement = (
                self._join_filter_sources(select(column, func.count()))
                .where(*conditions)
                .group_by(column)
            )
            facets[facet] = {
                str(value): count
                for value, count in self.session.execute(statement).all()
                if value is not None
            }

        return facets

    def get_interview(
        self,
        project_id: UUID4,
        interview_id: UUID4,
        full: bool = False,
    ) -> InterviewPublic:
        """Fetch an interview scoped to its project. Raises NoResultFound.

        `full=True` includes the transcript. Without it the messages are not
        merely skipped but explicitly `noload`ed: `InterviewPublic` declares
        the field, and Pydantic reads every declared attribute, so a lazy
        relationship would load the whole transcript no matter what the caller
        asked for. `n_messages` therefore comes from SQL rather than
        `len(messages)`, which would be 0 under noload.
        """
        options = [
            # test_name walks test_run -> test_setup; join it rather than
            # emitting two lazy loads per interview.
            joinedload(InterviewTable.test_run).joinedload(TestRunTable.test_setup),
            selectinload(InterviewTable.messages)
            .selectinload(MessageTable.annotations)
            .selectinload(MessageAnnotationTable.values)
            if full
            else noload(InterviewTable.messages),
        ]

        statement = (
            select(InterviewTable, InterviewTable.n_messages.label("n_messages"))
            .options(*options)
            .where(InterviewTable.project_id == project_id)
            .where(InterviewTable.id == interview_id)
        )
        interview, n_messages = self.session.execute(statement).one()

        return InterviewPublic.model_validate(interview).model_copy(
            update={"n_messages": n_messages}
        )

    def update_interview_status(
        self,
        project_id: UUID4,
        interview_id: UUID4,
        status: InterviewStatus | None = None,
        time_spent: int = 0,
    ):
        values: dict = {
            "last_updated": now(),
            "total_time_spent": InterviewTable.total_time_spent + time_spent,
        }
        if status is not None:
            values["status"] = status

        statement = (
            update(InterviewTable)
            .where(InterviewTable.project_id == project_id)
            .where(InterviewTable.id == interview_id)
            .values(**values)
        )
        self.session.execute(statement)
        self.session.commit()

    # ==================== Message Methods ====================

    @staticmethod
    def _message_options():
        """Eager-load what MessagePublic serializes.

        `MessagePublic` declares `annotations`, and `MessageAnnotationPublic`
        declares `values`, so validating a message emits a query for each --
        one per message, whether or not any annotations exist. selectinload
        collapses that into two queries for the whole result set.
        """
        return (
            selectinload(MessageTable.annotations).selectinload(
                MessageAnnotationTable.values
            ),
        )

    def insert_message(
        self,
        message_id: int,
        content: str,
        role: MessageRole,
        interview_id: UUID4,
        project_id: UUID4,
        message_type: MessageType = MessageType.TEXT,
        can_answer: bool = True,
        include_in_history: bool = True,
        attachment: Path | None = None,
        audio_file: str | None = None,
        survey_item: SurveyItem | None = None,
        image: Image | list[Image] | None = None,
        section: int | None = None,
        main_question: int | None = None,
        sub_question: int | None = None,
        is_introduction: bool = False,
        outro: bool = False,
        timed: bool = False,
        skipped_by_condition: bool = False,
    ) -> int:
        message = MessageTable(
            content=content,
            project_id=project_id,
            message_type=message_type,
            can_answer=can_answer,
            include_in_history=include_in_history,
            attachment=attachment,
            audio_file=audio_file,
            role=role,
            interview_id=interview_id,
            message_id=message_id,
            section=section,
            main_question=main_question,
            sub_question=sub_question,
            image=image,
            survey_item=survey_item,
            is_introduction=is_introduction,
            outro=outro,
            timed=timed,
            skipped_by_condition=skipped_by_condition,
        )
        self.session.add(message)
        try:
            self.session.execute(
                update(InterviewTable)
                .where(InterviewTable.id == interview_id)
                .where(InterviewTable.project_id == project_id)
                .values(last_updated=now())
            )
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            logger.warning(
                "Skipping duplicate message insert "
                "(project_id=%s interview_id=%s message_id=%s role=%s); "
                "an existing row with this (message_id, interview_id, project_id) "
                "is already persisted.",
                project_id,
                interview_id,
                message_id,
                role,
            )
            return message_id

        return message.message_id

    def save_image(self, image: Image): ...

    def update_feedback(
        self,
        message_id: int,
        interview_id: UUID4,
        feedback: Feedback | None,
    ):
        """Updates a message with feedback"""

        statement = (
            update(MessageTable)
            .where(MessageTable.message_id == message_id)
            .where(MessageTable.interview_id == interview_id)
            .values(feedback=feedback)
        )
        self.session.execute(statement)
        self.session.commit()

    def get_last_message(
        self,
        interview_id: UUID4,
        project_id: UUID4,
        role: MessageRole | None = None,
    ) -> MessagePublic | None:
        """Fetch the most recent message of an interview, optionally
        restricted to a role. Returns None if there are no matches."""
        statement = (
            select(MessageTable)
            .options(*self._message_options())
            .where(MessageTable.interview_id == interview_id)
            .where(MessageTable.project_id == project_id)
            .order_by(MessageTable.message_id.desc())
            .limit(1)
        )
        if role is not None:
            statement = statement.where(MessageTable.role == role)
        message = self.session.execute(statement).scalars().first()
        return MessagePublic.model_validate(message) if message else None

    def get_message(
        self,
        message_id: int,
        interview_id: UUID4,
        project_id: UUID4,
    ) -> MessagePublic:
        """Fetch a single message scoped to an interview. Raises NoResultFound."""
        statement = (
            select(MessageTable)
            .options(*self._message_options())
            .where(MessageTable.message_id == message_id)
            .where(MessageTable.interview_id == interview_id)
            .where(MessageTable.project_id == project_id)
        )
        message = self.session.execute(statement).scalar_one()
        return MessagePublic.model_validate(message)

    def get_messages(
        self,
        interview_id: UUID4,
        project_id: UUID4,
    ) -> list[MessagePublic]:
        statement = (
            select(MessageTable)
            .options(*self._message_options())
            .where(MessageTable.interview_id == interview_id)
            .where(MessageTable.project_id == project_id)
        )
        messages = self.session.execute(statement).scalars().all()
        return [MessagePublic.model_validate(message) for message in messages]

    # ==================== Task Methods ====================

    def insert_task(
        self,
        message_id: int,
        interview_id: UUID4,
        project_id: UUID4,
        task: str,
        reason: str | None = None,
        context: str | None = None,
        content: str | None = None,
        response: str | None = None,
        model: str | None = None,
        time_spend: int | None = None,
    ):
        new_task = TaskTable(
            message_id=message_id,
            interview_id=interview_id,
            project_id=project_id,
            task=task,
            reason=reason,
            context=context,
            content=content,
            response=response,
            model=model,
            time_spend=time_spend,
        )
        self.session.add(new_task)
        self.session.commit()

    # ==================== Interviewee Methods ====================

    def add_interviewee(self, project_id: UUID4, interviewee: IntervieweeCreate):
        self.session.add(
            IntervieweeTable(project_id=project_id, **interviewee.model_dump())
        )
        self.session.commit()

    def get_interviewee(
        self, project_id: UUID4, interview_id: UUID4
    ) -> IntervieweePublic:
        statement = select(IntervieweeTable).where(
            IntervieweeTable.project_id == project_id,
            IntervieweeTable.interview_id == interview_id,
        )
        interviewee = self.session.execute(statement).scalar_one()

        return IntervieweePublic.model_validate(interviewee)
