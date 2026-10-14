"""A project's content, stored once with its text per language.

The guide, consent and welcome live on one `projectcontent` row; a language is
a `projectlocalization` row saying the project offers it. These pin that each
language is read as a rendering of that one row, with untranslated text taken
from the default language, and that adding, removing and cloning keep the two
in step.
"""

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event, update
from sqlalchemy.exc import IntegrityError, NoResultFound
from sqlalchemy.orm import Session

from ainterviewer.interview_guides import (
    InterviewGuide,
    LocalizedConsent,
    LocalizedInterviewGuide,
    Question,
    QuestionSection,
    RadioItem,
)
from ainterviewer.interview_guides.conditions import (
    Condition,
    ConditionEvaluation,
    Conditions,
    QuestionContext,
)
from ainterviewer.interview_guides.types import ConditionAction
from app.api.dashboard import projects as projects_api
from app.db import InterviewDataBase
from app.db.repositories import project as project_repository
from app.db.repositories.errors import ProjectLanguageError
from app.db.repositories.project import render_default_guide
from app.db.tables import Base, ProjectContentTable, ProjectFolderTable, UserTable


@pytest.fixture
def db():
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def _enforce(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        user = UserTable(email="a@example.com", password="x", first_name="A")
        folder = ProjectFolderTable(title="f")
        session.add_all([user, folder])
        session.commit()
        database = InterviewDataBase(session)
        project_id = database.projects.create_project(
            folder_id=folder.id, title="p", owner_id=user.id, default_language="EN"
        )
        yield database, project_id


def _two_languages(database, project_id):
    """EN (default) and DA, with DA's text for every leaf but the outro."""
    en = InterviewGuide(
        introduction="Hello",
        outro="Bye",
        question_sections=[
            QuestionSection(
                description="Section",
                questions=[Question(main_question="How are you?")],
            )
        ],
    )
    guide = LocalizedInterviewGuide.from_rendered({"EN": en}, structure="EN")
    guide.introduction["DA"] = "Hej"
    guide.question_sections[0].description["DA"] = "Afsnit"
    guide.question_sections[0].questions[0].main_question["DA"] = "Hvordan har du det?"
    database.projects.update_guide(project_id, guide)
    database.projects.update_consent(
        project_id, LocalizedConsent(title={"EN": "Consent"}, text={"EN": "OK?"})
    )
    return guide


async def _add(database, project_id, language, translate=False):
    return await database.projects.add_project_language(project_id, language, translate)


def test_a_new_project_has_content_and_a_default_language(db):
    database, project_id = db

    localization = database.projects.get_project_localization(project_id, "EN")

    (section,) = localization.interview_guide.question_sections
    assert [q.main_question for q in section.questions] == [""]
    assert database.projects.get_default_language(project_id) == "EN"


@pytest.mark.anyio
async def test_a_language_renders_its_text_and_borrows_the_default(db):
    database, project_id = db
    await _add(database, project_id, "DA")
    _two_languages(database, project_id)

    da = database.projects.get_project_localization(project_id, "DA")

    assert da.interview_guide.introduction == "Hej"
    assert da.interview_guide.question_sections[0].questions[0].main_question == (
        "Hvordan har du det?"
    )
    # Untranslated: the default's text.
    assert da.interview_guide.outro == "Bye"
    assert da.consent is not None and da.consent.title == "Consent"


@pytest.mark.anyio
async def test_adding_a_language_translates_only_when_asked(db, monkeypatch):
    database, project_id = db
    calls = []

    async def fake(content, *, target, source, model):
        calls.append((type(content).__name__, target, source))
        return content

    monkeypatch.setattr(project_repository, "translate_missing", fake)

    await _add(database, project_id, "DA", translate=False)
    assert calls == []

    await _add(database, project_id, "SV", translate=True)
    # No consent or welcome yet, so only the guide.
    assert calls == [("LocalizedInterviewGuide", "SV", "EN")]


@pytest.mark.anyio
async def test_removing_a_language_removes_its_text(db):
    database, project_id = db
    await _add(database, project_id, "DA")
    _two_languages(database, project_id)

    database.projects.remove_project_language(project_id, "DA")

    guide = database.projects.get_guide(project_id)
    assert "DA" not in guide.languages()
    assert guide.introduction == {"EN": "Hello"}


@pytest.mark.anyio
async def test_a_clone_gets_its_own_copy_of_the_content(db):
    database, project_id = db
    await _add(database, project_id, "DA")
    guide = _two_languages(database, project_id)
    owner = database.projects.get_project(project_id).owner_id

    clone = database.projects.clone_project(project_id, owner)
    edited = guide.model_copy(deep=True)
    edited.introduction["EN"] = "Changed"
    database.projects.update_guide(project_id, edited)

    assert database.projects.get_guide(clone.id) == guide
    languages = database.projects.get_available_languages_optimized(clone.id)
    assert {language["code"] for language in languages} == {"EN", "DA"}


def test_analysis_reads_the_default_language(db):
    database, project_id = db
    _two_languages(database, project_id)

    rendered = render_default_guide(database.session, project_id)

    assert rendered is not None
    assert rendered.introduction == "Hello"


@pytest.mark.anyio
async def test_saving_a_condition_on_a_later_question_is_rejected(db):
    database, project_id = db
    later = Question(main_question="Later", survey_item=RadioItem(options=["Yes"]))
    gated = Question(
        main_question="Gated",
        conditions=Conditions(
            action=ConditionAction.SKIP_QUESTION,
            conditions=[
                Condition(
                    question_context=QuestionContext(
                        question_index=(0, 1), question_id=later.id
                    ),
                    evaluation=[ConditionEvaluation(trigger_value="Yes")],
                )
            ],
        ),
    )
    guide = LocalizedInterviewGuide.from_rendered(
        {
            "EN": InterviewGuide(
                question_sections=[
                    QuestionSection(description="", questions=[gated, later])
                ]
            )
        },
        structure="EN",
    )

    with pytest.raises(HTTPException) as error:
        await projects_api.update_guide(project_id, guide, database, None, None)  # ty: ignore[invalid-argument-type]

    assert error.value.status_code == 422


@pytest.mark.anyio
async def test_emails_are_text_per_language(db):
    database, project_id = db
    await _add(database, project_id, "DA")
    projects = database.projects

    projects.set_participant_email_template(project_id, "DA", "Hej", "<p>Hej</p>")
    projects.set_participant_email_template(project_id, "EN", "Hi", "<p>Hi</p>")
    projects.set_participant_reminder_email_template(project_id, "DA", None, "<p>!</p>")

    assert projects.get_participant_email_template(project_id, "DA") == (
        "Hej",
        "<p>Hej</p>",
    )
    # The default first, then the rest; a language with nothing reads as None.
    assert projects.get_participant_email_templates_ordered(project_id) == [
        ("EN", "Hi", "<p>Hi</p>"),
        ("DA", "Hej", "<p>Hej</p>"),
    ]
    assert projects.get_participant_reminder_email_templates_ordered(project_id) == [
        ("EN", None, None),
        ("DA", None, "<p>!</p>"),
    ]

    # Clearing a field removes it rather than storing an empty translation.
    projects.set_participant_email_template(project_id, "DA", None, None)
    assert projects.get_participant_email_template(project_id, "DA") == (None, None)

    projects.remove_project_language(project_id, "DA")
    content = projects._content(project_id)
    assert "DA" not in content.emails.reminder.template


def test_an_email_in_a_language_not_offered_is_not_found(db):
    database, project_id = db
    with pytest.raises(NoResultFound):
        database.projects.get_participant_email_template(project_id, "DA")


@pytest.mark.anyio
async def test_the_default_must_be_offered_and_cannot_be_removed(db):
    database, project_id = db
    projects = database.projects

    with pytest.raises(ProjectLanguageError):
        projects.set_default_language(project_id, "DA")
    with pytest.raises(ProjectLanguageError):
        projects.remove_project_language(project_id, "EN")

    await _add(database, project_id, "DA")
    languages = projects.set_default_language(project_id, "DA")

    assert {lang["code"] for lang in languages if lang["is_default"]} == {"DA"}
    projects.remove_project_language(project_id, "EN")
    assert projects.get_default_language(project_id) == "DA"


def test_the_database_holds_the_default_to_an_offered_language(db):
    database, project_id = db
    with pytest.raises(IntegrityError):
        database.session.execute(
            update(ProjectContentTable)
            .where(ProjectContentTable.project_id == project_id)
            .values(default_language="DA")
        )
