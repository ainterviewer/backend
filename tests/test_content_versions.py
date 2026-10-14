"""Versions of a project's content: publishing, snapshots, restore, pruning,
and the semantic version a publish gets.

These pin that respondents only ever see what was published, that a test runs
the draft without duplicating snapshots, and that the version number says
whether interviews on either side of it are comparable.
"""

import uuid

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from ainterviewer.interview_guides import (
    InterviewGuide,
    LocalizedConsent,
    LocalizedInterviewGuide,
    Question,
    QuestionSection,
    RadioItem,
)
from ainterviewer.interview_guides.localized import LocalizedQuestion
from app.db import InterviewDataBase
from app.db.repositories.errors import (
    ContentRevisionError,
    NotPublishedError,
    PublishError,
)
from app.db.tables import (
    Base,
    InterviewTable,
    ProjectContentTable,
    ProjectContentVersionTable,
    ProjectFolderTable,
    UserTable,
)
from app.db.types import ContentVersionKind, InterviewType
from app.db.versioning import Bump, classify


@pytest.fixture
def db():
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def _enforce(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        user = UserTable(email="a@example.com", password="x", first_name="Ada")
        folder = ProjectFolderTable(title="f")
        session.add_all([user, folder])
        session.commit()
        database = InterviewDataBase(session)
        project_id = database.projects.create_project(
            folder_id=folder.id, title="p", owner_id=user.id, default_language="EN"
        )
        database.projects.update_guide(project_id, _guide())
        yield database, project_id, user.id


def _guide() -> LocalizedInterviewGuide:
    en = InterviewGuide(
        introduction="Hello",
        question_sections=[
            QuestionSection(
                description="Section",
                questions=[
                    Question(main_question="How are you?"),
                    Question(
                        main_question="Pick one",
                        survey_item=RadioItem(options=["Yes", "No"]),
                    ),
                ],
            )
        ],
    )
    return LocalizedInterviewGuide.from_rendered({"EN": en}, structure="EN")


def _content(database, project_id) -> ProjectContentTable:
    return database.session.execute(
        select(ProjectContentTable).where(ProjectContentTable.project_id == project_id)
    ).scalar_one()


def _edit_guide(database, project_id, edit):
    guide = database.projects.get_guide(project_id).model_copy(deep=True)
    edit(guide)
    database.projects.update_guide(project_id, guide)


# ---- publishing --------------------------------------------------------------


def test_a_new_project_is_not_published(db):
    database, project_id, _ = db

    with pytest.raises(NotPublishedError):
        database.versions.published(project_id)
    status = database.versions.status(project_id)
    assert status.published is None
    assert status.has_unpublished_changes
    assert status.next_semver == "1.0.0"


def test_publishing_snapshots_the_draft_and_respondents_keep_it(db):
    database, project_id, user_id = db

    first = database.versions.publish(project_id, user_id, "Launch")
    assert (first.semver, first.label, first.created_by_name) == (
        "1.0.0",
        "Launch",
        "Ada",
    )
    assert first.is_published

    _edit_guide(database, project_id, lambda g: g.introduction.update(EN="Hi"))

    # The draft moved on; respondents did not.
    assert database.versions.published(project_id).guide.introduction == {"EN": "Hello"}
    assert database.versions.status(project_id).has_unpublished_changes


def test_publishing_an_unchanged_draft_changes_nothing(db):
    database, project_id, user_id = db
    first = database.versions.publish(project_id, user_id, None)

    again = database.versions.publish(project_id, user_id, None)

    assert again.id == first.id
    assert database.versions.list_versions(project_id, 10, 0)[1] == 1


def test_publishing_is_refused_while_a_language_lacks_text(db):
    database, project_id, user_id = db
    _content(database, project_id).languages = ["EN", "DA"]
    database.session.commit()

    with pytest.raises(PublishError) as raised:
        database.versions.publish(project_id, user_id, None)

    assert "guide.introduction" in raised.value.missing["DA"]
    assert "EN" not in raised.value.missing


def test_emails_count_towards_a_complete_language(db):
    database, project_id, _ = db
    database.projects.set_participant_email_template(
        project_id, "EN", "Hi", "<p>Hi</p>"
    )
    status = database.versions.status(project_id)
    assert status.missing == {}

    _content(database, project_id).languages = ["EN", "DA"]
    database.session.commit()

    missing = database.versions.status(project_id).missing["DA"]
    assert "emails.invite.subject" in missing


# ---- semantic versions -------------------------------------------------------


@pytest.mark.parametrize(
    ("edit", "expected"),
    [
        (lambda g: g.introduction.update(EN="Hi"), Bump.PATCH),
        (lambda g: g.introduction.update(DA="Hej"), Bump.PATCH),
        (
            lambda g: g.question_sections[0].questions.append(
                LocalizedQuestion(main_question={"EN": "New?"})
            ),
            Bump.MINOR,
        ),
        (lambda g: g.question_sections[0].questions.reverse(), Bump.MINOR),
        (
            lambda g: setattr(g.question_sections[0].questions[0], "max_probes_n", 1),
            Bump.MINOR,
        ),
        (lambda g: g.question_sections[0].questions.pop(0), Bump.MAJOR),
        (lambda g: g.question_sections.clear(), Bump.MAJOR),
        (
            lambda g: g.question_sections[0].questions[1].survey_item.options.pop(),
            Bump.MAJOR,
        ),
    ],
    ids=[
        "wording",
        "translation",
        "question added",
        "reordered",
        "setting",
        "question removed",
        "section removed",
        "option removed",
    ],
)
def test_the_bump_follows_whether_interviews_stay_comparable(db, edit, expected):
    database, project_id, _ = db
    before = _content(database, project_id)
    snapshot = database.versions.snapshot(project_id, ContentVersionKind.MANUAL)

    _edit_guide(database, project_id, edit)

    assert classify(snapshot, before) == expected


def test_adding_an_option_is_minor(db):
    database, project_id, _ = db
    snapshot = database.versions.snapshot(project_id, ContentVersionKind.MANUAL)

    def add(g):
        item = g.question_sections[0].questions[1].survey_item
        item.options.append(item.options[0].model_copy(update={"id": uuid.uuid4()}))

    _edit_guide(database, project_id, add)

    assert classify(snapshot, _content(database, project_id)) == Bump.MINOR


def test_consent_is_a_patch(db):
    database, project_id, _ = db
    snapshot = database.versions.snapshot(project_id, ContentVersionKind.MANUAL)

    database.projects.update_consent(
        project_id, LocalizedConsent(title={"EN": "Consent"}, text={"EN": "OK?"})
    )

    assert classify(snapshot, _content(database, project_id)) == Bump.PATCH


def test_versions_number_on_from_the_last_publish(db):
    database, project_id, user_id = db
    assert database.versions.publish(project_id, user_id, None).semver == "1.0.0"

    _edit_guide(database, project_id, lambda g: g.introduction.update(EN="Hi"))
    assert database.versions.status(project_id).pending_bump == "patch"
    assert database.versions.publish(project_id, user_id, None).semver == "1.0.1"

    _edit_guide(
        database,
        project_id,
        lambda g: g.question_sections[0].questions.append(LocalizedQuestion()),
    )
    assert database.versions.publish(project_id, user_id, None).semver == "1.1.0"

    _edit_guide(database, project_id, lambda g: g.question_sections[0].questions.pop())
    assert database.versions.publish(project_id, user_id, None).semver == "2.0.0"


def test_the_bump_can_be_raised_but_not_lowered(db):
    database, project_id, user_id = db
    database.versions.publish(project_id, user_id, None)

    _edit_guide(database, project_id, lambda g: g.introduction.update(EN="Hi"))
    assert (
        database.versions.publish(project_id, user_id, None, Bump.MAJOR).semver
        == "2.0.0"
    )

    _edit_guide(database, project_id, lambda g: g.question_sections[0].questions.pop())
    with pytest.raises(PublishError):
        database.versions.publish(project_id, user_id, None, Bump.MINOR)


def test_republishing_restored_content_still_moves_forward(db):
    database, project_id, user_id = db
    first = database.versions.publish(project_id, user_id, None)
    _edit_guide(database, project_id, lambda g: g.question_sections[0].questions.pop())
    database.versions.publish(project_id, user_id, None)  # 2.0.0

    database.versions.restore(project_id, first.id, user_id)

    # The question comes back: an addition against 2.0.0, numbered after it.
    assert database.versions.publish(project_id, user_id, None).semver == "2.1.0"


# ---- snapshots, restore, pruning --------------------------------------------


def test_test_snapshots_are_deduped_and_reuse_the_published_version(db):
    database, project_id, user_id = db
    published = database.versions.publish(project_id, user_id, None)

    # The draft is what was published: no snapshot of its own.
    assert (
        database.versions.snapshot(project_id, ContentVersionKind.TEST).id
        == published.id
    )

    _edit_guide(database, project_id, lambda g: g.introduction.update(EN="Hi"))
    one = database.versions.snapshot(project_id, ContentVersionKind.TEST)
    two = database.versions.snapshot(project_id, ContentVersionKind.TEST)

    assert one.id == two.id and one.kind == ContentVersionKind.TEST
    assert one.major is None


def test_restore_replaces_the_draft_and_keeps_what_it_replaced(db):
    database, project_id, user_id = db
    first = database.versions.publish(project_id, user_id, None)
    _edit_guide(database, project_id, lambda g: g.introduction.update(EN="Hi"))

    database.versions.restore(project_id, first.id, user_id)

    assert database.projects.get_guide(project_id).introduction == {"EN": "Hello"}
    (backup,) = [
        v
        for v in database.versions.list_versions(project_id, 10, 0)[0]
        if v.kind == ContentVersionKind.RESTORE
    ]
    assert backup.label == "Before restoring version 1"
    restored = database.versions.get_version(project_id, backup.id)
    assert restored.guide.introduction == {"EN": "Hi"}


def test_prune_keeps_saved_published_and_referenced_versions(db):
    database, project_id, user_id = db
    published = database.versions.publish(project_id, user_id, None)
    manual = database.versions.save_version(project_id, user_id, "Keep me")

    tests = []
    for n in range(5):
        _edit_guide(
            database, project_id, lambda g, n=n: g.introduction.update(EN=f"v{n}")
        )
        tests.append(database.versions.snapshot(project_id, ContentVersionKind.TEST))
    referenced = tests[0]
    database.session.add(
        InterviewTable(
            project_id=project_id,
            interview_guide=InterviewGuide(),
            type=InterviewType.MANUAL_TEST,
            content_version_id=referenced.id,
        )
    )
    database.session.commit()

    database.versions.prune(project_id, keep=1)
    database.session.commit()

    kept = set(
        database.session.execute(
            select(ProjectContentVersionTable.id).where(
                ProjectContentVersionTable.project_id == project_id
            )
        ).scalars()
    )
    assert {published.id, manual.id, referenced.id, tests[-1].id} == kept


# ---- revisions ---------------------------------------------------------------


def test_a_content_write_bumps_the_revision_but_publishing_does_not(db):
    database, project_id, user_id = db
    start = _content(database, project_id).revision

    database.versions.publish(project_id, user_id, None)
    assert _content(database, project_id).revision == start

    _edit_guide(database, project_id, lambda g: g.introduction.update(EN="Hi"))
    assert _content(database, project_id).revision == start + 1

    database.versions.check_revision(project_id, start + 1)
    with pytest.raises(ContentRevisionError) as raised:
        database.versions.check_revision(project_id, start)
    assert raised.value.current == start + 1
