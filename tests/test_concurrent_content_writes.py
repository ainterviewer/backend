"""Writes to a project's content that overlap another writer.

The guide editor's room saves every couple of seconds from its own session,
so anything else writing the content -- above all a translation, which waits
on an LLM between reading and writing -- overlaps it. These pin that such a
write neither reuses a revision the room already wrote (the room would never
see it) nor undoes what the room saved meanwhile.
"""

from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from ainterviewer.interview_guides import (
    InterviewGuide,
    LocalizedInterviewGuide,
    Question,
    QuestionSection,
)
from app.db import InterviewDataBase
from app.db.repositories import project as project_repository
from app.db.tables import Base, ProjectContentTable, ProjectFolderTable, UserTable
from app.db.types import ContentPart


@pytest.fixture
def db(tmp_path):
    """Two sessions must see each other's commits: a file, not `sqlite://`."""
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")

    @event.listens_for(engine, "connect")
    def _enforce(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)

    @contextmanager
    def session():
        with Session(engine) as s:
            yield InterviewDataBase(s)

    with session() as database:
        user = UserTable(email="a@example.com", password="x", first_name="A")
        folder = ProjectFolderTable(title="f")
        database.session.add_all([user, folder])
        database.session.commit()
        project_id = database.projects.create_project(
            folder_id=folder.id, title="p", owner_id=user.id, default_language="EN"
        )
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
        database.projects.update_guide(
            project_id,
            LocalizedInterviewGuide.from_rendered({"EN": en}, structure="EN"),
        )
    return session, project_id


def _content(session, project_id) -> ProjectContentTable:
    with session() as database:
        content = database.session.execute(
            select(ProjectContentTable).where(
                ProjectContentTable.project_id == project_id
            )
        ).scalar_one()
        database.session.expunge(content)
        return content


def _set_introduction(session, project_id, text: str) -> None:
    """What a room's save does: another session writing the guide."""
    with session() as database:
        guide = database.projects.get_guide(project_id).model_copy(deep=True)
        guide.introduction["EN"] = text
        database.projects.update_guide(project_id, guide)


def test_a_write_from_a_session_that_loaded_earlier_moves_the_revision_on(db):
    session, project_id = db
    with session() as stale:
        # Loaded, revision and all, as a request does before an LLM call.
        content = stale.projects._content(project_id)
        guide, loaded = content.guide.model_copy(deep=True), content.guide_revision

        _set_introduction(session, project_id, "From the room")
        saved = _content(session, project_id).guide_revision
        assert saved == loaded + 1

        guide.outro["EN"] = "From the stale session"
        stale.projects.update_guide(project_id, guide)

    # Never the room's own number again: the room would take the column as
    # still being what it saved, and its next save would overwrite this.
    assert _content(session, project_id).guide_revision == saved + 1


@pytest.mark.anyio
async def test_a_translation_keeps_what_editors_saved_while_it_ran(db, monkeypatch):
    session, project_id = db
    with session() as database:
        await database.projects.add_project_language(project_id, "DA", translate=False)

    async def translate(content, *, target, source, model):
        # The LLM call: an editor's room saves meanwhile.
        _set_introduction(session, project_id, "Hello there")
        translated = content.model_copy(deep=True)
        translated.introduction[target] = "Hej"
        translated.outro[target] = "Farvel"
        return translated

    monkeypatch.setattr(project_repository, "translate_missing", translate)

    with session() as database:
        # Loaded before translating, as the endpoint's language check does.
        database.projects.get_guide(project_id)
        await database.projects.translate_missing(project_id, "DA")

    guide = _content(session, project_id).guide
    assert guide.introduction == {"EN": "Hello there", "DA": "Hej"}
    assert guide.outro == {"EN": "Bye", "DA": "Farvel"}


@pytest.mark.anyio
async def test_adding_a_language_does_not_hold_the_write_lock_while_translating(
    db, monkeypatch
):
    session, project_id = db

    async def translate(content, *, target, source, model):
        # Would wait on the lock, and fail, if the language were not
        # committed before the LLM call.
        _set_introduction(session, project_id, "Hello there")
        return content

    monkeypatch.setattr(project_repository, "translate_missing", translate)

    with session() as database:
        await database.projects.add_project_language(project_id, "DA", translate=True)

    content = _content(session, project_id)
    assert content.languages == ["EN", "DA"]
    assert content.guide.introduction == {"EN": "Hello there"}


def test_errors_of_a_doc_the_column_has_moved_past_do_not_block_publishing(db):
    session, project_id = db
    with session() as database:
        revision = database.projects.get_revision(project_id, ContentPart.GUIDE)
        database.content_docs.save(
            project_id, ContentPart.GUIDE, b"", revision, None, ["broken"]
        )
        database.session.commit()
        assert database.versions.status(project_id).doc_errors == {
            ContentPart.GUIDE: ["broken"]
        }

    # A restore or a PUT, with no room open: the doc will become this
    # column's value when its room next opens.
    _set_introduction(session, project_id, "Restored")

    with session() as database:
        assert database.versions.status(project_id).doc_errors == {}
        database.versions.publish(project_id, _owner(database), None)


def _owner(database: InterviewDataBase):
    return database.session.execute(select(UserTable.id)).scalar_one()
