"""The participant list's server-side paging, filters, facets and selection."""

import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ainterviewer.interview_guides import InterviewGuide
from ainterviewer.types import InterviewStatus
from app.api.dashboard.participants import _selected_participant_ids
from app.db.crud import InterviewDataBase
from app.db.tables import (
    Base,
    InterviewTable,
    ParticipantTable,
    ProjectFolderTable,
    ProjectParticipantTable,
    ProjectTable,
    UserTable,
)
from app.schemas.participants import (
    ParticipantFilterParams,
    ParticipantSelection,
)
from app.types import InterviewType


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        user = UserTable(email="a@example.com", password="x", first_name="A")
        folder = ProjectFolderTable(title="f")
        project = ProjectTable(title="p", folder=folder, owner=user)
        other = ProjectTable(title="q", folder=folder, owner=user)
        session.add_all([user, folder, project, other])

        def participant(pid, name=None, participating=True, statuses=(), on=project):
            person = ParticipantTable(
                folder=folder, pid=pid, name=name, participating=participating
            )
            link = ProjectParticipantTable(project=on, participant=person)
            session.add_all([person, link])
            session.flush()
            for status in statuses:
                session.add(
                    InterviewTable(
                        project=on,
                        interview_guide=InterviewGuide(),
                        type=InterviewType.DISTRIBUTED,
                        status=status,
                        participant_id=link.id,
                    )
                )
            return link

        links = {
            "alice": participant("p1", "Alice", statuses=[InterviewStatus.COMPLETED]),
            "bob": participant("p2", "bob", statuses=[InterviewStatus.ACTIVE]),
            "carol": participant("p3", "Carol", participating=False),
            "nameless": participant("p4"),
            "other_project": participant("p5", "Alice", on=other),
        }
        session.commit()
        yield (
            InterviewDataBase(session),
            project.id,
            {name: link.id for name, link in links.items()},
        )


def ids(participants):
    return [p.id for p in participants]


def test_page_counts_the_project_and_slices_it(db):
    database, project_id, links = db
    page, total = database.participants.get_participant_page(
        project_id, offset=1, limit=2, sorting_column="pid", sorting_order="asc"
    )
    assert total == 4
    assert ids(page) == [links["bob"], links["carol"]]


def test_text_sorts_ignore_case_and_put_empty_last(db):
    database, project_id, links = db
    for order in ("asc", "desc"):
        page, _ = database.participants.get_participant_page(
            project_id, sorting_column="name", sorting_order=order
        )
        named = [links["alice"], links["bob"], links["carol"]]
        assert ids(page) == (named if order == "asc" else named[::-1]) + [
            links["nameless"]
        ]


def test_unknown_sort_column_is_rejected(db):
    database, project_id, _ = db
    with pytest.raises(ValueError):
        database.participants.get_participant_page(project_id, sorting_column="x")


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({"search": "ALI"}, ["alice"]),
        ({"search": "p2"}, ["bob"]),
        ({"participating": False}, ["carol"]),
        ({"statuses": [InterviewStatus.ACTIVE]}, ["bob"]),
        (
            {"statuses": [InterviewStatus.ACTIVE, InterviewStatus.COMPLETED]},
            ["alice", "bob"],
        ),
    ],
)
def test_filters_narrow_page_and_ids_alike(db, filters, expected):
    database, project_id, links = db
    page, total = database.participants.get_participant_page(project_id, **filters)
    assert set(ids(page)) == {links[name] for name in expected}
    assert total == len(expected)
    assert set(database.participants.get_participant_ids(project_id, **filters)) == {
        links[name] for name in expected
    }


def test_page_rows_carry_the_latest_interview(db):
    database, project_id, _ = db
    page, _ = database.participants.get_participant_page(project_id, search="p1")
    assert page[0].latest_interview_status == InterviewStatus.COMPLETED
    assert page[0].latest_interview_at is not None


def test_facets_count_with_every_other_filter_applied(db):
    database, project_id, _ = db
    facets = database.participants.get_participant_facets(
        project_id, participating=True, statuses=[InterviewStatus.ACTIVE]
    )
    # Its own filter is ignored, so the other values can still be picked;
    # participants without an interview have no status to offer.
    assert facets["status"] == {"active": 1, "completed": 1}
    assert facets["participating"] == {"true": 1}


@pytest.mark.parametrize(
    "body", [{}, {"participant_ids": [str(uuid.uuid4())], "filters": {}}]
)
def test_selection_needs_exactly_one_of_ids_or_filters(body):
    with pytest.raises(ValidationError):
        ParticipantSelection.model_validate(body)


def test_filter_selection_deletes_everything_matching(db):
    database, project_id, links = db
    request = ParticipantSelection(filters=ParticipantFilterParams(participating=True))
    database.participants.remove_participants(
        project_id, _selected_participant_ids(database, project_id, request)
    )
    assert database.participants.get_participant_ids(project_id) == [links["carol"]]
    # Another project's participant is out of reach of this project's filters.
    assert database.session.get(ProjectParticipantTable, links["other_project"])
