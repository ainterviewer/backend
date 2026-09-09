"""Tests that annotations, comments and categories stay inside their project.

Every one of these endpoints takes an id of its own -- a message, an
annotation, a comment, a category -- and the project the caller claims to be
acting in comes from the URL. The role check on the endpoint proves membership
of *that* project and nothing about the id, so the repository scopes each query
by the project as well: an id from somewhere else is simply not found.

These are the repository's half. `test_endpoint_authorization.py` is the other
half -- that the role check is there at all.
"""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from ainterviewer.types import MessageRole
from app.db.models import (
    AnalysisCategoryCreate,
    AnnotationValueCreate,
    MessageAnnotationCreate,
    MessageCommentCreate,
)
from app.db.repositories.analysis import AnalysisRepository
from app.db.tables import (
    Base,
    CollaboratorTable,
    MessageTable,
    ProjectFolderTable,
    ProjectTable,
    UserTable,
)
from app.db.types import AnnotationType
from app.types import CollaboratorRole, Scope

OURS = uuid.uuid4()
THEIRS = uuid.uuid4()


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def analysis(session):
    return AnalysisRepository(session)


def message(session, project_id: uuid.UUID) -> uuid.UUID:
    """One respondent message in a project, for things to hang off."""
    row = MessageTable(
        id=uuid.uuid4(),
        message_id=1,
        interview_id=uuid.uuid4(),
        project_id=project_id,
        role=MessageRole.USER,
        content="It has been a long year.",
    )
    session.add(row)
    session.flush()
    return row.id


def user(session, name: str = "Ada") -> uuid.UUID:
    """An author. Annotations and comments are returned with theirs attached."""
    row = UserTable(
        id=uuid.uuid4(),
        email=f"{name.lower()}-{uuid.uuid4().hex[:8]}@example.org",
        password="x",
        first_name=name,
    )
    session.add(row)
    session.flush()
    return row.id


def category(analysis, project_id: uuid.UUID, name: str = "Stress") -> uuid.UUID:
    created = analysis.create_analysis_category(
        AnalysisCategoryCreate(
            project_id=project_id,
            name=name,
            type=AnnotationType.TAG,
            color="#000000",
        )
    )
    return created.id


def annotate(analysis, project_id, message_id, user_id, category_id):
    return analysis.add_message_annotation(
        project_id,
        MessageAnnotationCreate(
            message_id=message_id,
            user_id=user_id,
            values=[AnnotationValueCreate(category_id=category_id, value_int=1)],
        ),
    )


class TestAnnotationsStayInTheirProject:
    def test_reading_another_project_s_message_finds_nothing(self, session, analysis):
        theirs = message(session, THEIRS)

        with pytest.raises(NoResultFound):
            analysis.get_message_annotations(OURS, theirs)

    def test_annotating_another_project_s_message_is_refused(self, session, analysis):
        theirs = message(session, THEIRS)
        ours = category(analysis, OURS)

        with pytest.raises(NoResultFound):
            annotate(analysis, OURS, theirs, user(session), ours)

    def test_a_category_from_another_project_is_refused(self, session, analysis):
        """The categories arrive in the payload rather than the URL, so the
        project has to be checked on them too -- otherwise an annotation can be
        coded with a category nobody here can see."""
        ours = message(session, OURS)
        theirs = category(analysis, THEIRS)

        with pytest.raises(NoResultFound):
            annotate(analysis, OURS, ours, user(session), theirs)

    def test_editing_from_the_wrong_project_finds_nothing(self, session, analysis):
        theirs = message(session, THEIRS)
        their_category = category(analysis, THEIRS)
        author = user(session)
        annotation = annotate(analysis, THEIRS, theirs, author, their_category)

        with pytest.raises(NoResultFound):
            analysis.delete_message_annotation(OURS, annotation.id)
        with pytest.raises(NoResultFound):
            analysis.can_modify_annotation(OURS, annotation.id, author, Scope.USER)

    def test_the_right_project_still_works(self, session, analysis):
        ours = message(session, OURS)
        annotation = annotate(
            analysis, OURS, ours, user(session), category(analysis, OURS)
        )

        assert [a.id for a in analysis.get_message_annotations(OURS, ours)] == [
            annotation.id
        ]


class TestWhoMayModifyAnAnnotation:
    """Its author, or somebody who moderates the project -- the same rule
    comments follow, so a project can clean up after a member who has left."""

    def owned_project(self, session, owner_id):
        folder = ProjectFolderTable(id=uuid.uuid4(), title="Folder")
        session.add(folder)
        session.add(
            ProjectTable(
                id=OURS, folder_id=folder.id, title="Project", owner_id=owner_id
            )
        )
        session.flush()
        return folder

    def test_the_author_may(self, session, analysis):
        author = user(session)
        self.owned_project(session, uuid.uuid4())
        annotation = annotate(
            analysis, OURS, message(session, OURS), author, category(analysis, OURS)
        )

        assert analysis.can_modify_annotation(OURS, annotation.id, author, Scope.USER)

    def test_another_member_may_not(self, session, analysis):
        self.owned_project(session, uuid.uuid4())
        annotation = annotate(
            analysis,
            OURS,
            message(session, OURS),
            user(session),
            category(analysis, OURS),
        )

        assert not analysis.can_modify_annotation(
            OURS, annotation.id, uuid.uuid4(), Scope.USER
        )

    def test_a_moderator_may(self, session, analysis):
        moderator = user(session, "Grace")
        folder = self.owned_project(session, uuid.uuid4())
        session.add(
            CollaboratorTable(
                id=uuid.uuid4(),
                folder_id=folder.id,
                user_id=moderator,
                role=CollaboratorRole.ADMIN,
            )
        )
        annotation = annotate(
            analysis,
            OURS,
            message(session, OURS),
            user(session),
            category(analysis, OURS),
        )

        assert analysis.can_modify_annotation(
            OURS, annotation.id, moderator, Scope.USER
        )


class TestCommentsStayInTheirProject:
    def test_reading_another_project_s_message_finds_nothing(self, session, analysis):
        theirs = message(session, THEIRS)

        with pytest.raises(NoResultFound):
            analysis.get_message_comments(OURS, theirs)

    def test_commenting_on_another_project_s_message_is_refused(
        self, session, analysis
    ):
        theirs = message(session, THEIRS)

        with pytest.raises(NoResultFound):
            analysis.add_message_comment(
                OURS, theirs, user(session), MessageCommentCreate(body="Hello")
            )

    def test_editing_from_the_wrong_project_finds_nothing(self, session, analysis):
        theirs = message(session, THEIRS)
        comment = analysis.add_message_comment(
            THEIRS, theirs, user(session), MessageCommentCreate(body="Hello")
        )

        with pytest.raises(NoResultFound):
            analysis.update_message_comment(OURS, comment.id, "Changed")
        with pytest.raises(NoResultFound):
            analysis.delete_message_comment(OURS, comment.id)


class TestCategoriesStayInTheirProject:
    def test_renaming_another_project_s_category_finds_nothing(self, analysis):
        theirs = category(analysis, THEIRS)

        with pytest.raises(NoResultFound):
            analysis.update_analysis_category(
                OURS,
                theirs,
                AnalysisCategoryCreate(
                    project_id=OURS,
                    name="Renamed",
                    type=AnnotationType.TAG,
                    color="#000000",
                ),
            )

    def test_deleting_another_project_s_category_finds_nothing(self, analysis):
        theirs = category(analysis, THEIRS)

        with pytest.raises(NoResultFound):
            analysis.delete_analysis_category(OURS, theirs)

    def test_an_update_cannot_move_a_category_to_another_project(self, analysis):
        """`project_id` rides along in the payload, and writing it back
        verbatim would carry the category past the role check that has already
        run against the project in the URL."""
        ours = category(analysis, OURS)

        updated = analysis.update_analysis_category(
            OURS,
            ours,
            AnalysisCategoryCreate(
                project_id=THEIRS,
                name="Renamed",
                type=AnnotationType.TAG,
                color="#000000",
            ),
        )

        assert updated.project_id == OURS
        assert [c.id for c in analysis.get_analysis_categories(THEIRS)] == []
