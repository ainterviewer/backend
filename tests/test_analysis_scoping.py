"""Tests that the codebook, codings and comments stay inside their project.

Every one of these endpoints takes an id of its own -- a message, a code, a
coding, a comment -- and the project the caller claims to be acting in comes
from the URL. The role check on the endpoint proves membership of *that*
project and nothing about the id, so the repository scopes each query by the
project as well: an id from somewhere else is simply not found.

These are the repository's half. `test_endpoint_authorization.py` is the other
half -- that the role check is there at all.
"""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from ainterviewer.types import MessageRole
from app.db.models import CodeBase, CodebookPut, CodingCreate, MessageCommentCreate
from app.db.repositories.analysis import AnalysisRepository
from app.db.repositories.errors import CodebookError, CodingError
from app.db.tables import (
    Base,
    CollaboratorTable,
    MessageTable,
    ProjectFolderTable,
    ProjectTable,
    UserTable,
)
from app.db.types import CodeKind
from app.types import CollaboratorRole, Scope

FOLDER = uuid.uuid4()
OURS = uuid.uuid4()
THEIRS = uuid.uuid4()

CONTENT = "It has been a long year."


@pytest.fixture
def session():
    """Two projects in one folder, and nothing else.

    The projects are real rows rather than bare ids because a codebook is
    stored partly *on* the project -- its palette -- so a test against an id
    with no project behind it would pass while saving nothing.
    """
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        folder = ProjectFolderTable(id=FOLDER, title="Folder")
        session.add(folder)
        for project_id in (OURS, THEIRS):
            session.add(
                ProjectTable(
                    id=project_id,
                    folder_id=FOLDER,
                    title=f"Project {project_id}",
                    owner_id=uuid.uuid4(),
                )
            )
        session.flush()
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
        content=CONTENT,
    )
    session.add(row)
    session.flush()
    return row.id


def user(session, name: str = "Ada") -> uuid.UUID:
    """An author. Codings and comments are returned with theirs attached."""
    row = UserTable(
        id=uuid.uuid4(),
        email=f"{name.lower()}-{uuid.uuid4().hex[:8]}@example.org",
        password="x",
        first_name=name,
    )
    session.add(row)
    session.flush()
    return row.id


def code(
    analysis,
    project_id: uuid.UUID,
    name: str = "Stress",
    kind: CodeKind = CodeKind.TAG,
    **extra,
) -> uuid.UUID:
    """One more code in a project's codebook, keeping the ones already there.

    A save replaces the whole codebook, so a helper that sent only its own code
    would silently delete every earlier one -- and the tests that need two
    codes would be testing a codebook of one.
    """
    kept = [
        CodeBase(**existing.model_dump(exclude={"created_at", "updated_at"}))
        for existing in analysis.get_codebook(project_id).codes
    ]
    new = CodeBase(id=uuid.uuid4(), name=name, kind=kind, color="#000000", **extra)
    analysis.save_codebook(
        project_id, CodebookPut(codes=[*kept, new], palette=["#000000"])
    )
    return new.id


def apply(analysis, project_id, message_id, user_id, code_id, **extra):
    return analysis.add_message_coding(
        project_id, message_id, user_id, CodingCreate(code_id=code_id, **extra)
    )


class TestCodingsStayInTheirProject:
    def test_reading_another_project_s_message_finds_nothing(self, session, analysis):
        theirs = message(session, THEIRS)

        with pytest.raises(NoResultFound):
            analysis.get_message_codings(OURS, theirs)

    def test_coding_another_project_s_message_is_refused(self, session, analysis):
        theirs = message(session, THEIRS)
        ours = code(analysis, OURS)

        with pytest.raises(NoResultFound):
            apply(analysis, OURS, theirs, user(session), ours)

    def test_a_code_from_another_project_is_refused(self, session, analysis):
        """The code arrives in the payload rather than the URL, so the project
        has to be checked on it too -- otherwise a passage can be coded with a
        code nobody here can see."""
        ours = message(session, OURS)
        theirs = code(analysis, THEIRS)

        with pytest.raises(CodingError):
            apply(analysis, OURS, ours, user(session), theirs)

    def test_editing_from_the_wrong_project_finds_nothing(self, session, analysis):
        theirs = message(session, THEIRS)
        author = user(session)
        coding = apply(analysis, THEIRS, theirs, author, code(analysis, THEIRS))

        with pytest.raises(NoResultFound):
            analysis.delete_message_coding(OURS, coding.id)
        with pytest.raises(NoResultFound):
            analysis.can_modify_coding(OURS, coding.id, author, Scope.USER)

    def test_the_right_project_still_works(self, session, analysis):
        ours = message(session, OURS)
        coding = apply(analysis, OURS, ours, user(session), code(analysis, OURS))

        assert [c.id for c in analysis.get_message_codings(OURS, ours)] == [coding.id]


class TestWhatMakesACodingMeaningful:
    """Each of these would store a claim the coder did not make, so each is a
    refusal rather than a silent repair."""

    def test_a_group_is_never_applied(self, session, analysis):
        group = code(analysis, OURS, "Barriers", CodeKind.GROUP)

        with pytest.raises(CodingError, match="group"):
            apply(analysis, OURS, message(session, OURS), user(session), group)

    def test_a_score_needs_a_value_on_its_own_scale(self, session, analysis):
        score = code(
            analysis, OURS, "Elaboration", CodeKind.SCORE, min_value=1, max_value=5
        )
        ours = message(session, OURS)
        author = user(session)

        with pytest.raises(CodingError, match="needs a value"):
            apply(analysis, OURS, ours, author, score)
        with pytest.raises(CodingError, match="outside"):
            apply(analysis, OURS, ours, author, score, value_int=9)

        assert apply(analysis, OURS, ours, author, score, value_int=4).value_int == 4

    def test_a_tag_takes_no_value(self, session, analysis):
        tag = code(analysis, OURS)

        with pytest.raises(CodingError, match="takes no value"):
            apply(
                analysis, OURS, message(session, OURS), user(session), tag, value_int=1
            )

    def test_a_span_has_to_fall_inside_the_message(self, session, analysis):
        tag = code(analysis, OURS)
        ours = message(session, OURS)
        author = user(session)

        with pytest.raises(CodingError, match="span"):
            apply(analysis, OURS, ours, author, tag, start_offset=0, end_offset=999)
        with pytest.raises(CodingError, match="both a start and an end"):
            apply(analysis, OURS, ours, author, tag, start_offset=0)

        span = apply(analysis, OURS, ours, author, tag, start_offset=0, end_offset=5)
        assert (span.start_offset, span.end_offset) == (0, 5)

    def test_the_whole_message_and_a_span_are_different_codings(
        self, session, analysis
    ):
        """Both offsets NULL is "this turn is about X"; a span is "these words
        are". The second is not a duplicate of the first."""
        tag = code(analysis, OURS)
        ours = message(session, OURS)
        author = user(session)

        apply(analysis, OURS, ours, author, tag)
        apply(analysis, OURS, ours, author, tag, start_offset=0, end_offset=5)

        assert len(analysis.get_message_codings(OURS, ours)) == 2

    def test_one_coder_cannot_code_one_passage_twice(self, session, analysis):
        """The whole-message case is two NULL offsets, which SQL counts as
        distinct from each other -- so a unique constraint would let exactly
        the case a UI actually sends through."""
        tag = code(analysis, OURS)
        ours = message(session, OURS)
        author = user(session)
        apply(analysis, OURS, ours, author, tag)

        with pytest.raises(CodingError, match="already carries"):
            apply(analysis, OURS, ours, author, tag)

    def test_another_coder_may_code_the_same_passage(self, session, analysis):
        """Two coders agreeing is the point of coding twice, not a duplicate."""
        tag = code(analysis, OURS)
        ours = message(session, OURS)
        apply(analysis, OURS, ours, user(session, "Ada"), tag)
        apply(analysis, OURS, ours, user(session, "Grace"), tag)

        assert len(analysis.get_message_codings(OURS, ours)) == 2


class TestSavingACodebook:
    def test_list_order_is_sibling_order_and_survives_a_reorder(
        self, session, analysis
    ):
        first, second = uuid.uuid4(), uuid.uuid4()
        sent = [
            CodeBase(id=first, name="Trust"),
            CodeBase(id=second, name="Barriers"),
        ]
        analysis.save_codebook(OURS, CodebookPut(codes=sent, palette=[]))

        saved = analysis.save_codebook(
            OURS, CodebookPut(codes=list(reversed(sent)), palette=[])
        )
        assert [code.name for code in saved.codes] == ["Barriers", "Trust"]

    def test_a_branch_reads_depth_first(self, session, analysis):
        root, child, grandchild, second = (uuid.uuid4() for _ in range(4))
        saved = analysis.save_codebook(
            OURS,
            CodebookPut(
                codes=[
                    CodeBase(id=root, name="Trust"),
                    CodeBase(id=child, parent_id=root, name="Institutional"),
                    CodeBase(id=grandchild, parent_id=child, name="Doctors"),
                    CodeBase(id=second, name="Barriers"),
                ],
                palette=[],
            ),
        )

        assert [code.name for code in saved.codes] == [
            "Trust",
            "Institutional",
            "Doctors",
            "Barriers",
        ]

    def test_a_code_that_survives_keeps_its_codings(self, session, analysis):
        """Ids are the client's, so a save is a diff. If it were a rewrite,
        every save during a coding session would throw the session away."""
        kept = code(analysis, OURS)
        ours = message(session, OURS)
        apply(analysis, OURS, ours, user(session), kept)

        code(analysis, OURS, "Something else")

        assert len(analysis.get_message_codings(OURS, ours)) == 1

    def test_a_code_left_out_is_deleted_with_its_codings(self, session, analysis):
        doomed = code(analysis, OURS)
        ours = message(session, OURS)
        apply(analysis, OURS, ours, user(session), doomed)

        analysis.save_codebook(OURS, CodebookPut(codes=[], palette=[]))

        assert analysis.get_codebook(OURS).codes == []
        assert analysis.get_message_codings(OURS, ours) == []

    def test_turning_a_coded_code_into_a_group_is_refused(self, session, analysis):
        """Groups are never applied, so this would drop somebody's coding --
        a decision for them rather than a side effect of a kind change."""
        coded = code(analysis, OURS)
        apply(analysis, OURS, message(session, OURS), user(session), coded)

        with pytest.raises(CodebookError, match="cannot become a group"):
            analysis.save_codebook(
                OURS,
                CodebookPut(
                    codes=[CodeBase(id=coded, name="Stress", kind=CodeKind.GROUP)],
                    palette=[],
                ),
            )

    def test_a_range_does_not_outlive_the_score_it_belonged_to(self, session, analysis):
        """A range left on a tag reappears if the code is made a score again,
        carrying the scale somebody deliberately changed."""
        scored = uuid.uuid4()
        analysis.save_codebook(
            OURS,
            CodebookPut(
                codes=[
                    CodeBase(
                        id=scored,
                        name="Elaboration",
                        kind=CodeKind.SCORE,
                        min_value=1,
                        max_value=5,
                    )
                ],
                palette=[],
            ),
        )

        saved = analysis.save_codebook(
            OURS,
            CodebookPut(
                codes=[
                    CodeBase(
                        id=scored,
                        name="Elaboration",
                        kind=CodeKind.TAG,
                        min_value=1,
                        max_value=5,
                    )
                ],
                palette=[],
            ),
        )
        assert (saved.codes[0].min_value, saved.codes[0].max_value) == (None, None)

    @pytest.mark.parametrize(
        ("label", "codes"),
        [
            (
                "a parent that is not in the codebook",
                lambda ids: [CodeBase(id=ids[0], parent_id=ids[1], name="Orphan")],
            ),
            (
                "a code that is its own parent",
                lambda ids: [CodeBase(id=ids[0], parent_id=ids[0], name="Trust")],
            ),
            (
                "a cycle",
                lambda ids: [
                    CodeBase(id=ids[0], parent_id=ids[1], name="Trust"),
                    CodeBase(id=ids[1], parent_id=ids[0], name="Institutional"),
                ],
            ),
            (
                "the same code twice",
                lambda ids: [
                    CodeBase(id=ids[0], name="Trust"),
                    CodeBase(id=ids[0], name="Trust again"),
                ],
            ),
        ],
    )
    def test_a_codebook_that_is_not_a_tree_is_refused(
        self, session, analysis, label, codes
    ):
        ids = [uuid.uuid4(), uuid.uuid4()]

        with pytest.raises(CodebookError):
            analysis.save_codebook(OURS, CodebookPut(codes=codes(ids), palette=[]))

    def test_the_palette_round_trips_and_defaults_when_unsaved(self, session, analysis):
        assert analysis.get_codebook(OURS).palette  # never empty: the default

        analysis.save_codebook(OURS, CodebookPut(codes=[], palette=["#123456"]))
        assert analysis.get_codebook(OURS).palette == ["#123456"]

    def test_one_project_s_codebook_is_not_another_s(self, session, analysis):
        code(analysis, OURS, "Ours")
        code(analysis, THEIRS, "Theirs")

        assert [c.name for c in analysis.get_codebook(OURS).codes] == ["Ours"]
        assert [c.name for c in analysis.get_codebook(THEIRS).codes] == ["Theirs"]


class TestCountingCodings:
    def test_a_parent_does_not_inherit_its_children_s(self, session, analysis):
        """A passage coded `Cost` is not thereby coded `Barriers`; that is what
        `GROUP` is for."""
        parent = uuid.uuid4()
        child = uuid.uuid4()
        analysis.save_codebook(
            OURS,
            CodebookPut(
                codes=[
                    CodeBase(id=parent, name="Barriers", kind=CodeKind.GROUP),
                    CodeBase(id=child, parent_id=parent, name="Cost"),
                ],
                palette=[],
            ),
        )
        apply(analysis, OURS, message(session, OURS), user(session), child)

        assert analysis.count_codings_by_code(OURS) == {child: 1}


class TestWhoMayModifyACoding:
    """Its author, or somebody who moderates the project -- the same rule
    comments follow, so a project can clean up after a member who has left."""

    def test_the_author_may(self, session, analysis):
        author = user(session)
        coding = apply(
            analysis, OURS, message(session, OURS), author, code(analysis, OURS)
        )

        assert analysis.can_modify_coding(OURS, coding.id, author, Scope.USER)

    def test_another_member_may_not(self, session, analysis):
        coding = apply(
            analysis, OURS, message(session, OURS), user(session), code(analysis, OURS)
        )

        assert not analysis.can_modify_coding(OURS, coding.id, uuid.uuid4(), Scope.USER)

    def test_a_moderator_may(self, session, analysis):
        moderator = user(session, "Grace")
        session.add(
            CollaboratorTable(
                id=uuid.uuid4(),
                folder_id=FOLDER,
                user_id=moderator,
                role=CollaboratorRole.ADMIN,
            )
        )
        coding = apply(
            analysis, OURS, message(session, OURS), user(session), code(analysis, OURS)
        )

        assert analysis.can_modify_coding(OURS, coding.id, moderator, Scope.USER)


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


class TestReadingCodingsForManyMessages:
    """The lookup explore uses to draw what a page of turns is already coded
    as, in one request rather than one per turn."""

    def test_keys_by_message_and_leaves_out_the_uncoded(self, session, analysis):
        tag = code(analysis, OURS)
        coded = message(session, OURS)
        bare = message(session, OURS)
        apply(analysis, OURS, coded, user(session), tag)

        found = analysis.get_codings_for_messages(OURS, [coded, bare])

        assert list(found) == [coded]
        assert [c.code_id for c in found[coded]] == [tag]

    def test_a_message_from_another_project_is_absent(self, session, analysis):
        theirs = message(session, THEIRS)
        apply(analysis, THEIRS, theirs, user(session), code(analysis, THEIRS))

        assert analysis.get_codings_for_messages(OURS, [theirs]) == {}

    def test_no_messages_is_no_query(self, session, analysis):
        assert analysis.get_codings_for_messages(OURS, []) == {}
