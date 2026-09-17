"""Tests for the two review queues over respondents' reports.

A report carries two independent statuses -- the project member's and the
platform admin's -- and the whole point of that design is that neither
reviewer can clear the other's queue. That invariant is the first thing here,
because nothing else about the feature fails as quietly: an owner marking a
question "resolved, I reworded it" must not take a safety report out of the
platform's review.

The rest is the scoping (`ids` arrive in a request body, so a project's calls
are confined to that project's reports), and the read state, which is
per-reviewer and must not leak between them.
"""

import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ainterviewer.interview_guides import InterviewGuide
from ainterviewer.types import MessageRole
from app.db.models import MessageReportCreate
from app.db.repositories.interview import InterviewRepository
from app.db.repositories.reports import ReportRepository
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

FOLDER = uuid.uuid4()
OURS = uuid.uuid4()
THEIRS = uuid.uuid4()
QUESTION = "How much do you earn?"


@pytest.fixture
def session():
    """Two projects, each with one interview holding one question."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(ProjectFolderTable(id=FOLDER, title="Folder"))
        for project_id in (OURS, THEIRS):
            session.add(
                ProjectTable(
                    id=project_id,
                    folder_id=FOLDER,
                    title=f"Project {project_id}",
                    owner_id=uuid.uuid4(),
                )
            )
            session.add(
                InterviewTable(
                    id=project_id,  # one interview per project; same id, fine
                    project_id=project_id,
                    interview_guide=InterviewGuide(),
                    type=InterviewType.DISTRIBUTED,
                )
            )
            session.add(
                MessageTable(
                    id=uuid.uuid4(),
                    message_id=1,
                    interview_id=project_id,
                    project_id=project_id,
                    role=MessageRole.ASSISTANT,
                    content=QUESTION,
                )
            )
        session.commit()
        yield session


@pytest.fixture
def reports(session):
    return ReportRepository(session)


@pytest.fixture
def interviews(session):
    return InterviewRepository(session)


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


def report(interviews, project_id=OURS, reason=ReportReason.OFFENSIVE, comment=None):
    return interviews.create_message_report(
        interview_id=project_id,
        project_id=project_id,
        report=MessageReportCreate(message_id=1, reason=reason, comment=comment),
    )


def test_resolving_one_track_leaves_the_other_open(reports, interviews, session):
    """The invariant the two-track design exists for."""
    saved = report(interviews)
    owner, admin = user(session, "Owner"), user(session, "Admin")

    assert reports.resolve(owner, [saved.id], ReportStatus.RESOLVED, "owner") == 1

    row = session.get(MessageReportTable, saved.id)
    assert row is not None
    assert row.status is ReportStatus.RESOLVED
    assert row.resolved_by_id == owner
    assert row.resolved_at is not None
    # Untouched: still in the platform's queue.
    assert row.admin_status is ReportStatus.OPEN
    assert row.admin_resolved_by_id is None
    assert row.admin_resolved_at is None

    assert reports.resolve(admin, [saved.id], ReportStatus.DISMISSED, "admin") == 1

    session.refresh(row)
    assert row.admin_status is ReportStatus.DISMISSED
    assert row.admin_resolved_by_id == admin
    # The owner's resolution survives the admin's.
    assert row.status is ReportStatus.RESOLVED
    assert row.resolved_by_id == owner


def test_reopening_clears_the_resolver(reports, interviews, session):
    """A report that is open again was not resolved by anybody."""
    saved = report(interviews)
    owner = user(session, "Owner")

    reports.resolve(owner, [saved.id], ReportStatus.RESOLVED, "owner")
    reports.resolve(owner, [saved.id], ReportStatus.OPEN, "owner")

    row = session.get(MessageReportTable, saved.id)
    assert row is not None
    assert row.status is ReportStatus.OPEN
    assert row.resolved_by_id is None
    assert row.resolved_at is None


def test_a_project_cannot_resolve_another_project_s_report(
    reports, interviews, session
):
    """The ids come from the request body; the role check proves membership of
    the project in the path and nothing about them."""
    theirs = report(interviews, project_id=THEIRS)
    member = user(session)

    updated = reports.resolve(
        member, [theirs.id], ReportStatus.RESOLVED, "owner", project_id=OURS
    )

    assert updated == 0
    row = session.get(MessageReportTable, theirs.id)
    assert row is not None
    assert row.status is ReportStatus.OPEN


def test_a_project_cannot_mark_another_project_s_report_read(
    reports, interviews, session
):
    theirs = report(interviews, project_id=THEIRS)
    member = user(session)

    assert reports.mark_read(member, [theirs.id], project_id=OURS) == 0
    assert session.execute(select(MessageReportReadTable)).all() == []


def test_a_project_listing_shows_only_its_own_reports(reports, interviews, session):
    ours = report(interviews, project_id=OURS)
    report(interviews, project_id=THEIRS)
    member = user(session)

    listed = reports.list_reports(member, project_id=OURS)

    assert [row.id for row in listed] == [ours.id]


def test_the_admin_listing_spans_every_project(reports, interviews, session):
    ours = report(interviews, project_id=OURS)
    theirs = report(interviews, project_id=THEIRS)
    admin = user(session, "Admin")

    listed = reports.list_reports(admin, track="admin")

    assert {row.id for row in listed} == {ours.id, theirs.id}


def test_a_listed_report_carries_the_question_it_is_about(reports, interviews, session):
    """A queue of message ids is not something anyone can review."""
    report(interviews, comment="None of your business.")
    member = user(session)

    row = reports.list_reports(member, project_id=OURS)[0]

    assert row.question == QUESTION
    assert row.question_number == 1
    assert row.project_title == f"Project {OURS}"
    assert row.comment == "None of your business."


def test_read_state_is_per_reviewer(reports, interviews, session):
    """Two reviewers looking at the same report each see their own read state,
    or one member's visit would silently clear it for everybody."""
    saved = report(interviews)
    ada, grace = user(session, "Ada"), user(session, "Grace")

    assert reports.mark_read(ada, [saved.id], project_id=OURS) == 1

    (for_ada,) = reports.list_reports(ada, project_id=OURS)
    (for_grace,) = reports.list_reports(grace, project_id=OURS)

    assert for_ada.read_by_me is True
    assert for_grace.read_by_me is False


def test_marking_read_twice_does_not_move_the_timestamp(reports, interviews, session):
    """Idempotent: a client that marks the page read on every visit must not
    keep resetting when the reviewer first saw it."""
    saved = report(interviews)
    ada = user(session)

    assert reports.mark_read(ada, [saved.id], project_id=OURS) == 1
    first = session.execute(select(MessageReportReadTable.read_at)).scalar_one()

    assert reports.mark_read(ada, [saved.id], project_id=OURS) == 0

    assert session.execute(select(MessageReportReadTable.read_at)).scalar_one() == first
    assert len(session.execute(select(MessageReportReadTable)).all()) == 1


def test_unread_only_hides_what_this_reviewer_has_read(reports, interviews, session):
    first = report(interviews, reason=ReportReason.OFFENSIVE)
    second = report(interviews, reason=ReportReason.IRRELEVANT)
    ada = user(session)

    reports.mark_read(ada, [first.id], project_id=OURS)

    unread = reports.list_reports(ada, project_id=OURS, unread_only=True)

    assert [row.id for row in unread] == [second.id]


def test_a_status_filter_reads_the_named_track(reports, interviews, session):
    """`statuses` on the admin queue filters `admin_status`, so an owner's
    resolution does not hide a report the platform has not looked at."""
    saved = report(interviews)
    owner, admin = user(session, "Owner"), user(session, "Admin")

    reports.resolve(owner, [saved.id], ReportStatus.RESOLVED, "owner")

    still_open = reports.list_reports(
        admin, track="admin", statuses=[ReportStatus.OPEN]
    )
    assert [row.id for row in still_open] == [saved.id]

    owner_open = reports.list_reports(
        owner, project_id=OURS, track="owner", statuses=[ReportStatus.OPEN]
    )
    assert owner_open == []


def test_resolving_nothing_is_not_an_error(reports, session):
    assert reports.resolve(user(session), [], ReportStatus.RESOLVED, "owner") == 0
    assert reports.mark_read(user(session, "Bo"), []) == 0


# ------------------------------------------------------- the interviews list


def test_the_interview_list_counts_reported_questions(reports, interviews):
    report(interviews)
    report(interviews, reason=ReportReason.IRRELEVANT)

    rows, total = interviews.get_interviews(OURS)

    assert total == 1
    assert rows[0].n_reports == 2


def test_an_unreported_interview_counts_zero(interviews):
    rows, _ = interviews.get_interviews(OURS)

    assert rows[0].n_reports == 0


@pytest.mark.parametrize(
    ("reported", "expected"),
    [(True, 1), (False, 0), (None, 1)],
)
def test_the_reported_filter_selects_on_having_any(interviews, reported, expected):
    report(interviews)

    _, total = interviews.get_interviews(OURS, reported=reported)

    assert total == expected


def test_the_reported_facet_counts_both_answers(interviews, session):
    """The facet has to offer the two values the filter accepts, not every
    distinct count -- and a project with no reports at all still needs a
    "false" count, or the filter could never be widened again."""
    session.add(
        InterviewTable(
            id=(second := uuid.uuid4()),
            project_id=OURS,
            interview_guide=InterviewGuide(),
            type=InterviewType.DISTRIBUTED,
        )
    )
    session.commit()

    report(interviews)

    facets = interviews.get_interview_facets(OURS)

    assert facets["reported"] == {"true": 1, "false": 1}
    assert second is not None


def test_the_reported_facet_ignores_its_own_selection(interviews):
    """Counted with every *other* filter applied: with its own selection in
    place the unselected value would report zero and the dropdown could never
    be widened."""
    report(interviews)

    facets = interviews.get_interview_facets(OURS, reported=True)

    assert facets["reported"] == {"true": 1}


def test_interviews_can_be_sorted_by_report_count(interviews, session):
    session.add(
        InterviewTable(
            id=(quiet := uuid.uuid4()),
            project_id=OURS,
            interview_guide=InterviewGuide(),
            type=InterviewType.DISTRIBUTED,
        )
    )
    session.commit()
    report(interviews)

    rows, _ = interviews.get_interviews(
        OURS, sorting_column="n_reports", sorting_order="desc"
    )

    assert [row.n_reports for row in rows] == [1, 0]
    assert rows[1].id == quiet
