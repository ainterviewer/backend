"""Tests for a respondent reporting one interviewer question.

The write is scoped by the interview, not by the message: the respondent holds
no account, so the endpoint takes the interview and project from their
interview token and the repository looks the message up inside them. A
message_id from somebody else's interview is simply not found -- which is the
only thing standing between a respondent and another transcript, and so the
first thing tested here.

The rest is what the feature would quietly get wrong: that reports accumulate
rather than overwrite, that deleting an interview takes its reports with it
(SQLite does not enforce the cascade -- see
`BaseRepository._delete_message_children`), and that reading a transcript does
not emit a query per message now that `MessagePublic` declares `reports`.
"""

import datetime
import io
import uuid

import polars as pl
import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from ainterviewer.interview_guides import InterviewGuide
from ainterviewer.types import MessageRole
from app.db.models import MessagePublic, MessageReportCreate, MessageReportPublic
from app.db.repositories.interview import InterviewRepository
from app.db.tables import (
    Base,
    InterviewTable,
    MessageReportReadTable,
    MessageReportTable,
    MessageTable,
    ProjectFolderTable,
    ProjectTable,
    UserTable,
)
from app.db.types import InterviewType, ReportReason, ReportStatus
from app.db.utils import (
    fix_nested_columns,
    messages_to_dataframe,
    write_messages_xlsx,
)

FOLDER = uuid.uuid4()
PROJECT = uuid.uuid4()
OURS = uuid.uuid4()
THEIRS = uuid.uuid4()


@pytest.fixture
def engine():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session(engine):
    """One project with two interviews, each holding one question.

    Two interviews because that is the boundary the repository is supposed to
    enforce: both transcripts number their messages from 1, so `message_id=1`
    is ambiguous until the interview is named.
    """
    with Session(engine) as session:
        session.add(ProjectFolderTable(id=FOLDER, title="Folder"))
        session.add(
            ProjectTable(
                id=PROJECT,
                folder_id=FOLDER,
                title="Project",
                owner_id=uuid.uuid4(),
            )
        )
        for interview_id in (OURS, THEIRS):
            session.add(
                InterviewTable(
                    id=interview_id,
                    project_id=PROJECT,
                    interview_guide=InterviewGuide(),
                    type=InterviewType.DISTRIBUTED,
                )
            )
            session.add(
                MessageTable(
                    id=uuid.uuid4(),
                    message_id=1,
                    interview_id=interview_id,
                    project_id=PROJECT,
                    role=MessageRole.ASSISTANT,
                    content="How much do you earn?",
                )
            )
        session.commit()
        yield session


@pytest.fixture
def interviews(session):
    return InterviewRepository(session)


def report(
    interviews,
    interview_id=OURS,
    reason=ReportReason.INAPPROPRIATE,
    comment=None,
    message_id=1,
):
    return interviews.create_message_report(
        interview_id=interview_id,
        project_id=PROJECT,
        report=MessageReportCreate(
            message_id=message_id, reason=reason, comment=comment
        ),
    )


def user(session, name: str = "Ada") -> uuid.UUID:
    row = UserTable(
        id=uuid.uuid4(),
        email=f"{name.lower()}-{uuid.uuid4().hex[:8]}@example.org",
        password="x",
        first_name=name,
    )
    session.add(row)
    session.flush()
    return row.id


def test_a_report_lands_on_the_message_of_the_named_interview(interviews, session):
    saved = report(interviews, comment="This is none of your business.")

    ours = session.execute(
        select(MessageTable.id).where(MessageTable.interview_id == OURS)
    ).scalar_one()

    assert saved.message_id == ours
    assert saved.interview_id == OURS
    assert saved.reason is ReportReason.INAPPROPRIATE
    assert saved.comment == "This is none of your business."


def test_both_review_tracks_start_open(interviews, session):
    """Asserted on the row, not on what the respondent is handed back.

    `MessageReportPublic` deliberately carries only the project track -- the
    platform's review is internal, and a model that could serialize it to a
    respondent or a project member is the thing that must not exist. The
    defaults still belong to the row, so that is where they are checked.
    """
    saved = report(interviews)

    assert saved.status is ReportStatus.OPEN
    assert saved.resolved_by_id is None
    assert saved.resolved_at is None
    assert not [field for field in saved.model_dump() if field.startswith("admin_")]

    row = session.get(MessageReportTable, saved.id)
    assert row is not None
    assert row.admin_status is ReportStatus.OPEN
    assert row.admin_resolved_by_id is None
    assert row.admin_resolved_at is None


def test_a_message_id_from_another_interview_is_not_found(interviews, session):
    """Both interviews have a message 1. Naming one from the other's token
    must not reach it -- this is the whole of the access control."""
    with pytest.raises(NoResultFound):
        interviews.create_message_report(
            interview_id=uuid.uuid4(),  # an interview that is not the caller's
            project_id=PROJECT,
            report=MessageReportCreate(message_id=1, reason=ReportReason.OFFENSIVE),
        )

    assert session.execute(select(MessageReportTable)).all() == []


def test_a_message_id_that_does_not_exist_is_not_found(interviews):
    with pytest.raises(NoResultFound):
        report(interviews, message_id=999)


def test_reports_accumulate_rather_than_overwrite(interviews, session):
    """Reporting the same question twice is two reports: a respondent who
    reports, reconsiders and reports again has said something a reviewer
    should see."""
    first = report(interviews, reason=ReportReason.IRRELEVANT)
    second = report(interviews, reason=ReportReason.OFFENSIVE, comment="Actually...")

    assert first.id != second.id
    stored = session.execute(
        select(MessageReportTable.reason).order_by(MessageReportTable.created_at)
    ).scalars()
    assert set(stored) == {ReportReason.IRRELEVANT, ReportReason.OFFENSIVE}


@pytest.mark.parametrize("comment", ["", "   ", "\n\t "])
def test_a_blank_comment_is_stored_as_no_comment(interviews, comment):
    """The dialog submits its comment field whether or not the respondent
    typed in it; a queue must not show a report that appears to carry a note
    and does not."""
    assert report(interviews, comment=comment).comment is None


def test_deleting_an_interview_deletes_its_reports_and_reads(interviews, session):
    """SQLite does not enforce the cascade here, so the delete path lists
    these explicitly -- see `BaseRepository._delete_message_children`."""
    saved = report(interviews)
    session.add(
        MessageReportReadTable(
            id=uuid.uuid4(), report_id=saved.id, user_id=user(session)
        )
    )
    session.commit()

    interviews.delete_interviews(PROJECT, [OURS])

    assert session.execute(select(MessageReportTable)).all() == []
    assert session.execute(select(MessageReportReadTable)).all() == []


def test_deleting_an_interview_leaves_another_s_reports_alone(interviews, session):
    ours = report(interviews, interview_id=OURS)
    theirs = report(interviews, interview_id=THEIRS)

    interviews.delete_interviews(PROJECT, [OURS])

    remaining = session.execute(select(MessageReportTable.id)).scalars().all()
    assert remaining == [theirs.id]
    assert ours.id not in remaining


def test_reading_a_transcript_does_not_query_per_message(engine):
    """`MessagePublic` declares `reports`, so Pydantic reads the relationship
    on every message. `_message_options` eager-loads it; without that this
    grows with the transcript, which is the trap that method exists to close.
    """

    def queries_for(n_messages: int) -> int:
        with Session(engine) as session:
            folder_id = uuid.uuid4()
            project_id = uuid.uuid4()
            interview_id = uuid.uuid4()
            session.add(ProjectFolderTable(id=folder_id, title="Folder"))
            session.add(
                ProjectTable(
                    id=project_id,
                    folder_id=folder_id,
                    title=f"Project {project_id}",
                    owner_id=uuid.uuid4(),
                )
            )
            session.add(
                InterviewTable(
                    id=interview_id,
                    project_id=project_id,
                    interview_guide=InterviewGuide(),
                    type=InterviewType.DISTRIBUTED,
                )
            )
            for index in range(1, n_messages + 1):
                session.add(
                    MessageTable(
                        id=uuid.uuid4(),
                        message_id=index,
                        interview_id=interview_id,
                        project_id=project_id,
                        role=MessageRole.ASSISTANT,
                        content=f"Question {index}",
                    )
                )
            session.commit()

            repository = InterviewRepository(session)
            count = 0

            def counter(*_args, **_kwargs):
                nonlocal count
                count += 1

            event.listen(engine, "before_cursor_execute", counter)
            try:
                messages = repository.get_messages(interview_id, project_id)
            finally:
                event.remove(engine, "before_cursor_execute", counter)

            assert len(messages) == n_messages
            return count

    assert queries_for(2) == queries_for(20)


def test_a_reported_question_exports_as_text():
    """`MessagePublic.reports` is a list, and csv holds no nested data.

    The csv path would cope on its own -- it runs `fix_nested_columns` over
    whatever is left -- but that renders an unreported message as "[]", and
    the xlsx path does not run it at all. Both rows matter: the mixed case,
    one reported message and one not, is what polars types the column from.
    """
    when = datetime.datetime(2026, 9, 17, 12, 0, tzinfo=datetime.UTC)
    interview_id, project_id = uuid.uuid4(), uuid.uuid4()

    def message(reports):
        return MessagePublic(
            id=uuid.uuid4(),
            message_id=1,
            content="How much do you earn?",
            role=MessageRole.ASSISTANT,
            interview_id=interview_id,
            project_id=project_id,
            created_at=when,
            interview_type=InterviewType.DISTRIBUTED,
            reports=reports,
        )

    reported = MessageReportPublic(
        id=uuid.uuid4(),
        message_id=uuid.uuid4(),
        interview_id=interview_id,
        project_id=project_id,
        reason=ReportReason.OFFENSIVE,
        comment="None of your business.",
        created_at=when,
        updated_at=when,
        status=ReportStatus.OPEN,
    )

    frame = messages_to_dataframe([message([reported]), message([])])

    assert frame.schema["reports"] == pl.String
    # The unreported message carries an empty cell, not "[]" or "null".
    assert frame["reports"].to_list()[1] == ""

    # The flat columns are what can actually be counted and filtered in a
    # spreadsheet; the JSON keeps the whole record beside them.
    assert frame["n_reports"].to_list() == [1, 0]
    assert frame["report_reasons"].to_list() == ["offensive", ""]

    # Through the endpoint's own csv path, nested columns and all.
    csv = fix_nested_columns(frame).write_csv()
    assert "None of your business." in csv

    # xlsx goes through a different writer, which runs no such fixup.
    buffer = io.BytesIO()
    write_messages_xlsx(frame, buffer)
    assert buffer.getvalue()


def test_several_reports_on_one_question_export_as_one_row():
    """A question may be reported more than once, and for different reasons.
    The reasons are joined rather than given a column each, which would be
    mostly empty and still not say how many."""
    when = datetime.datetime(2026, 9, 17, 12, 0, tzinfo=datetime.UTC)
    interview_id, project_id = uuid.uuid4(), uuid.uuid4()

    def report_row(reason):
        return MessageReportPublic(
            id=uuid.uuid4(),
            message_id=uuid.uuid4(),
            interview_id=interview_id,
            project_id=project_id,
            reason=reason,
            created_at=when,
            updated_at=when,
            status=ReportStatus.OPEN,
        )

    frame = messages_to_dataframe(
        [
            MessagePublic(
                id=uuid.uuid4(),
                message_id=1,
                content="How much do you earn?",
                role=MessageRole.ASSISTANT,
                interview_id=interview_id,
                project_id=project_id,
                created_at=when,
                interview_type=InterviewType.DISTRIBUTED,
                reports=[
                    report_row(ReportReason.IRRELEVANT),
                    report_row(ReportReason.OFFENSIVE),
                ],
            )
        ]
    )

    assert frame.height == 1
    assert frame["n_reports"].to_list() == [2]
    assert frame["report_reasons"].to_list() == ["irrelevant; offensive"]
