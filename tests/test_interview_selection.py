"""Bulk interview actions on "everything matching the list's filters"."""

import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ainterviewer.interview_guides import InterviewGuide
from ainterviewer.types import InterviewStatus
from app.api.dashboard.projects import _filter_kwargs, _selected_interview_ids
from app.api.request_models import (
    DeleteInterviewRequest,
    ExportMessagesRequest,
    InterviewFilters,
)
from app.db import InterviewDataBase
from app.db.tables import (
    Base,
    InterviewTable,
    ProjectFolderTable,
    ProjectTable,
    UserTable,
)
from app.db.types import InterviewType


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        user = UserTable(email="a@example.com", password="x", first_name="A")
        folder = ProjectFolderTable(title="f")
        project = ProjectTable(title="p", folder=folder, owner=user)
        other = ProjectTable(title="q", folder=folder, owner=user)

        def interview(status, type=InterviewType.DISTRIBUTED, project=project):
            return InterviewTable(
                project=project,
                interview_guide=InterviewGuide(),
                type=type,
                status=status,
            )

        interviews = {
            "completed": interview(InterviewStatus.COMPLETED),
            "inactive": interview(InterviewStatus.INACTIVE),
            "test": interview(
                InterviewStatus.COMPLETED, type=InterviewType.SYNTHETIC_TEST
            ),
            "other_project": interview(InterviewStatus.COMPLETED, project=other),
        }
        session.add_all([user, folder, project, other, *interviews.values()])
        session.commit()
        yield (
            InterviewDataBase(session),
            project.id,
            {name: row.id for name, row in interviews.items()},
        )


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"interview_ids": [str(uuid.uuid4())], "filters": {}},
    ],
)
@pytest.mark.parametrize("model", [DeleteInterviewRequest, ExportMessagesRequest])
def test_selection_needs_exactly_one_of_ids_or_filters(model, body):
    with pytest.raises(ValidationError):
        model.model_validate(body)


def test_empty_filters_select_the_distributed_interviews_of_the_project(db):
    database, project_id, ids = db
    selected = database.interviews.get_interview_ids(
        project_id, **_filter_kwargs(InterviewFilters())
    )
    # Not the test run (outside the list's default scope), and not another
    # project's interview.
    assert set(selected) == {ids["completed"], ids["inactive"]}


def test_filters_narrow_the_selection(db):
    database, project_id, ids = db
    selected = database.interviews.get_interview_ids(
        project_id,
        **_filter_kwargs(InterviewFilters(statuses=[InterviewStatus.INACTIVE])),
    )
    assert selected == [ids["inactive"]]


def test_scope_reaches_test_runs(db):
    database, project_id, ids = db
    selected = database.interviews.get_interview_ids(
        project_id,
        **_filter_kwargs(
            InterviewFilters(interview_types=[InterviewType.SYNTHETIC_TEST])
        ),
    )
    assert selected == [ids["test"]]


def test_ids_are_passed_through_untouched(db):
    database, project_id, ids = db
    request = DeleteInterviewRequest(interview_ids=[ids["completed"]])
    assert _selected_interview_ids(database, project_id, request) == [ids["completed"]]


def test_filter_selection_deletes_everything_matching(db):
    database, project_id, ids = db
    request = DeleteInterviewRequest(filters=InterviewFilters())
    database.interviews.delete_interviews(
        project_id, _selected_interview_ids(database, project_id, request)
    )
    remaining = database.interviews.get_interview_ids(
        project_id,
        **_filter_kwargs(
            InterviewFilters(
                interview_types=[
                    InterviewType.DISTRIBUTED,
                    InterviewType.SYNTHETIC_TEST,
                ]
            )
        ),
    )
    assert remaining == [ids["test"]]


@pytest.mark.parametrize("timezone", ["Mars/Olympus", "../etc/passwd"])
def test_unknown_timezone_is_rejected(timezone):
    with pytest.raises(ValidationError):
        ExportMessagesRequest(interview_ids=[], format="pdf", timezone=timezone)
