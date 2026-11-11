"""Sending respondents on to a panel's return link when an interview ends."""

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ainterviewer.interview_guides import InterviewGuide
from ainterviewer.types import InterviewEndReason, InterviewStatus
from app.db.crud import InterviewDataBase
from app.db.tables import (
    Base,
    InterviewTable,
    ProjectFolderTable,
    ProjectTable,
    UserTable,
)
from app.types import CompletionRedirects, InterviewType, render_redirect_url

COMPLETE = "https://panel.example/z/Complete?id={{ rid }}"
SCREENED = "https://panel.example/z/Screened"


# ── URL validation ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://panel.example/z/Complete",
        "https://panel.example/done?id={{ rid }}&src={{src}}",
        "https://panel.example/{{ rid }}/done#{{ rid }}",
    ],
)
def test_accepts_https_urls_with_placeholders(url):
    assert CompletionRedirects(complete=url).complete == url


@pytest.mark.parametrize(
    "url",
    [
        "http://panel.example/done",
        "javascript:alert(1)",
        "/relative/path",
        "https://{{ host }}/done",
        "https://panel.{{ host }}.example/done",
        "https://panel.example/done?id={{ rid | upper }}",
        "https://panel.example/{% if x %}a{% endif %}",
        "https://panel.example/done?id={{ rid }",
    ],
)
def test_rejects_other_schemes_hosts_from_params_and_other_jinja(url):
    with pytest.raises(ValidationError):
        CompletionRedirects(complete=url)


def test_only_finished_and_condition_endings_redirect():
    redirects = CompletionRedirects(complete=COMPLETE, screened=SCREENED)

    assert redirects.url_for(InterviewEndReason.FINISHED) == COMPLETE
    assert redirects.url_for(InterviewEndReason.CONDITION) == SCREENED
    assert redirects.url_for(InterviewEndReason.SECURITY) is None
    assert redirects.url_for(None) is None


# ── Rendering ──────────────────────────────────────────────────────────────


def test_values_are_url_encoded():
    url, missing = render_redirect_url(COMPLETE, {"rid": "a b&c=d/e"})

    assert url == "https://panel.example/z/Complete?id=a%20b%26c%3Dd%2Fe"
    assert missing == set()


def test_a_missing_value_is_filled_in_empty_and_reported():
    url, missing = render_redirect_url(COMPLETE, {})

    assert url == "https://panel.example/z/Complete?id="
    assert missing == {"rid"}


# ── Resolving an interview's redirect ──────────────────────────────────────


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        user = UserTable(email="a@example.com", password="x", first_name="A")
        folder = ProjectFolderTable(title="f")
        project = ProjectTable(
            title="p",
            folder=folder,
            owner=user,
            redirects=CompletionRedirects(
                complete=COMPLETE, screened=SCREENED, delay_seconds=3
            ),
        )
        bare = ProjectTable(title="q", folder=folder, owner=user)
        session.add_all([user, folder, project, bare])
        session.commit()

        def interview(
            end_reason=InterviewEndReason.FINISHED,
            status=InterviewStatus.COMPLETED,
            type=InterviewType.DISTRIBUTED,
            project=project,
            external_params=None,
        ):
            row = InterviewTable(
                project=project,
                interview_guide=InterviewGuide(),
                type=type,
                status=status,
                end_reason=end_reason,
                external_params=external_params,
            )
            session.add(row)
            session.commit()
            return project.id, row.id

        yield InterviewDataBase(session), interview, bare


def test_a_finished_interview_goes_to_the_complete_url(db):
    database, interview, _ = db
    project_id, interview_id = interview(external_params={"rid": "R1"})

    frame = database.interviews.completion_redirect(project_id, interview_id)

    assert frame is not None
    assert frame.url == "https://panel.example/z/Complete?id=R1"
    assert frame.delay_seconds == 3
    assert frame.model_dump(mode="json")["type"] == "redirect"


def test_a_screened_out_interview_goes_to_the_screened_url(db):
    database, interview, _ = db
    frame = database.interviews.completion_redirect(
        *interview(end_reason=InterviewEndReason.CONDITION)
    )

    assert frame is not None
    assert frame.url == SCREENED


@pytest.mark.parametrize(
    "kwargs",
    [
        {"end_reason": InterviewEndReason.SECURITY},
        # Completed before the reason was recorded.
        {"end_reason": None},
        {"status": InterviewStatus.INACTIVE, "end_reason": None},
        {"type": InterviewType.MANUAL_TEST},
        {"type": InterviewType.SYNTHETIC_TEST},
    ],
)
def test_no_redirect(db, kwargs):
    database, interview, _ = db

    assert database.interviews.completion_redirect(*interview(**kwargs)) is None


def test_no_redirect_for_a_project_without_one(db):
    database, interview, bare = db

    assert database.interviews.completion_redirect(*interview(project=bare)) is None


def test_completing_records_the_end_reason_and_reopening_clears_it(db):
    database, interview, _ = db
    project_id, interview_id = interview(status=InterviewStatus.ACTIVE, end_reason=None)

    def stored():
        return database.interviews.session.get(InterviewTable, interview_id)

    database.update_interview_status(
        project_id,
        interview_id,
        status=InterviewStatus.COMPLETED,
        end_reason=InterviewEndReason.CONDITION,
    )
    database.interviews.session.expire_all()
    assert stored().end_reason == InterviewEndReason.CONDITION

    database.update_interview_status(
        project_id, interview_id, status=InterviewStatus.INACTIVE
    )
    database.interviews.session.expire_all()
    assert stored().end_reason is None
