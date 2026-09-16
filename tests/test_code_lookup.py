"""Placing a `code:` reference in a project's codebook.

`test_keyword_query.py` covers what a reference *parses* to; this covers what
it then means. The two halves are deliberately apart: a parser has no project,
and everything here needs one.

The error messages are part of the contract, the same way the grammar's are. A
name that cannot be placed is shown to a reader who then has to fix it, so
saying *which* name, and what to write instead, is the whole job.
"""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.code_lookup import CodeIndex, resolve_all
from app.db.keyword_query import And, CodeTerm, KeywordQueryError, Node, parse
from app.db.tables import Base, CodeTable, ProjectFolderTable, ProjectTable
from app.db.types import CodeKind


def tree(query: str) -> Node:
    """`parse`, for a query that is known to be one.

    `parse` widens to None for a query that asks nothing, and none of these
    are that -- asserting it here keeps the tests about resolution.
    """
    node = parse(query)
    assert node is not None
    return node


FOLDER = uuid.uuid4()
OURS = uuid.uuid4()
THEIRS = uuid.uuid4()

# One codebook, arranged to hold every case worth asking about: a plain code
# with children, a GROUP that only organises, a name that collides three ways,
# and a branch deep enough that a path can be a tail rather than a whole
# descent.
STRESS = uuid.uuid4()
OFTEN = uuid.uuid4()
RARELY = uuid.uuid4()
WORKLOAD = uuid.uuid4()
HOURS = uuid.uuid4()
TOP = uuid.uuid4()
MID = uuid.uuid4()
LEAF = uuid.uuid4()
NEW_TOP = uuid.uuid4()
NEW_UNDER_STRESS = uuid.uuid4()
NEW_UNDER_WORKLOAD = uuid.uuid4()
SHARED_UNDER_OFTEN = uuid.uuid4()
SHARED_UNDER_HOURS = uuid.uuid4()
TWIN_A = uuid.uuid4()
TWIN_B = uuid.uuid4()
THEIR_SECRET = uuid.uuid4()


@pytest.fixture
def session():
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
        codes = [
            (STRESS, None, "Stress", CodeKind.TAG, OURS),
            (OFTEN, STRESS, "Often", CodeKind.TAG, OURS),
            (RARELY, STRESS, "Rarely", CodeKind.TAG, OURS),
            (WORKLOAD, None, "Workload", CodeKind.GROUP, OURS),
            (HOURS, WORKLOAD, "Hours", CodeKind.SCORE, OURS),
            (TOP, None, "Top", CodeKind.GROUP, OURS),
            (MID, TOP, "Mid", CodeKind.GROUP, OURS),
            (LEAF, MID, "Leaf", CodeKind.TAG, OURS),
            (NEW_TOP, None, "New code", CodeKind.TAG, OURS),
            (NEW_UNDER_STRESS, STRESS, "New code", CodeKind.TAG, OURS),
            (NEW_UNDER_WORKLOAD, WORKLOAD, "New code", CodeKind.TAG, OURS),
            # Colliding with no root-level namesake, so no full path can settle
            # it and the name is genuinely ambiguous.
            (SHARED_UNDER_OFTEN, OFTEN, "Shared", CodeKind.TAG, OURS),
            (SHARED_UNDER_HOURS, HOURS, "Shared", CodeKind.TAG, OURS),
            # Siblings with one name, which is what clicking "+ Code" twice
            # and renaming neither leaves behind. No path tells them apart.
            (TWIN_A, None, "Twin", CodeKind.TAG, OURS),
            (TWIN_B, None, "Twin", CodeKind.TAG, OURS),
            (THEIR_SECRET, None, "Secret", CodeKind.TAG, THEIRS),
        ]
        for code_id, parent_id, name, kind, project_id in codes:
            session.add(
                CodeTable(
                    id=code_id,
                    project_id=project_id,
                    parent_id=parent_id,
                    name=name,
                    kind=kind,
                )
            )
        session.commit()
        yield session


@pytest.fixture
def index(session):
    return CodeIndex.for_project(session, OURS)


def term(query: str) -> CodeTerm:
    node = parse(query)
    assert isinstance(node, CodeTerm)
    return node


class TestPlacingAName:
    def test_a_unique_name(self, index):
        assert index.resolve(term("code:Often")) == (OFTEN,)

    def test_matching_ignores_case(self, index):
        assert index.resolve(term("code:often")) == (OFTEN,)
        assert index.resolve(term("code:OFTEN")) == (OFTEN,)

    def test_a_path_picks_one_of_a_colliding_name(self, index):
        assert index.resolve(term('code:"Stress/New code"')) == (NEW_UNDER_STRESS,)
        assert index.resolve(term('code:"Workload/New code"')) == (NEW_UNDER_WORKLOAD,)

    def test_a_path_is_a_tail_and_need_not_start_at_the_root(self, index):
        """A reader disambiguating should add as much of the branch as it takes
        and no more."""
        assert index.resolve(term("code:Mid/Leaf")) == (LEAF,)
        assert index.resolve(term("code:Top/Mid/Leaf")) == (LEAF,)

    def test_a_path_that_does_not_line_up_is_not_found(self, index):
        with pytest.raises(KeywordQueryError):
            index.resolve(term("code:Top/Leaf"))

    def test_another_project_is_a_different_codebook(self, index):
        with pytest.raises(KeywordQueryError) as raised:
            index.resolve(term("code:Secret"))
        assert "No code in this project" in raised.value.message


class TestSubtrees:
    def test_a_bare_name_is_that_code_alone(self, index):
        assert index.resolve(term("code:Stress")) == (STRESS,)

    def test_a_star_takes_the_branch(self, index):
        assert set(index.resolve(term("code:Stress/*"))) == {
            STRESS,
            OFTEN,
            RARELY,
            NEW_UNDER_STRESS,
            SHARED_UNDER_OFTEN,
        }

    def test_a_star_reaches_past_one_level(self, index):
        assert set(index.resolve(term("code:Top/*"))) == {TOP, MID, LEAF}

    def test_a_star_on_a_leaf_is_just_the_leaf(self, index):
        assert index.resolve(term("code:Rarely/*")) == (RARELY,)


class TestGroups:
    def test_a_group_asked_for_exactly_is_refused(self, index):
        """A GROUP is never applied, so the query cannot match -- and the reader
        almost certainly meant the branch."""
        with pytest.raises(KeywordQueryError) as raised:
            index.resolve(term("code:Workload"))
        assert "only groups the codes under it" in raised.value.message
        assert 'code:"Workload"/*' in raised.value.message

    def test_a_group_with_a_star_is_fine(self, index):
        assert set(index.resolve(term("code:Workload/*"))) == {
            WORKLOAD,
            HOURS,
            NEW_UNDER_WORKLOAD,
            SHARED_UNDER_HOURS,
        }


class TestSpecificity:
    """A whole descent from the root beats a tail of one."""

    def test_a_full_path_wins_over_a_tail(self, index):
        """Without this the root-level "New code" is unnameable: every tail its
        name appears in matches it too, so the shortest way to write it is also
        the most ambiguous one."""
        assert index.resolve(term('code:"New code"')) == (NEW_TOP,)

    def test_a_tail_still_works_where_nothing_is_fully_qualified(self, index):
        assert index.resolve(term("code:Mid/Leaf")) == (LEAF,)


class TestAmbiguity:
    def test_it_says_how_many_and_offers_the_paths(self, index):
        with pytest.raises(KeywordQueryError) as raised:
            index.resolve(term("code:Shared"))
        message = raised.value.message
        assert "2 codes are called" in message
        # Named rather than merely counted: a reader told only that the name is
        # ambiguous still has to go and look.
        assert 'code:"Stress/Often/Shared"' in message
        assert 'code:"Workload/Hours/Shared"' in message

    @pytest.mark.parametrize("suggestion_index", [0, 1])
    def test_every_path_offered_actually_resolves(self, index, suggestion_index):
        """The suggestions have to be ones a reader can paste back in -- which
        is the whole reason a full path beats a tail."""
        with pytest.raises(KeywordQueryError) as raised:
            index.resolve(term("code:Shared"))
        offered = raised.value.message.split('code:"')[1:]
        path = offered[suggestion_index].split('"')[0]
        assert len(index.resolve(term(f'code:"{path}"'))) == 1


class TestUnnameableSiblings:
    """Codes in the same place with the same name.

    Found on a real codebook, where three top-level codes were all still called
    "New code": every suggestion the error offered was the string that had just
    failed.
    """

    def test_it_says_there_is_no_path_rather_than_offering_one(self, index):
        with pytest.raises(KeywordQueryError) as raised:
            index.resolve(term("code:Twin"))
        message = raised.value.message
        assert "in the same place" in message
        assert "rename one" in message
        assert 'code:"' not in message

    def test_a_distinguishable_collision_still_gets_paths(self, index):
        """The honest message is for the case that has no answer, not for every
        collision."""
        with pytest.raises(KeywordQueryError) as raised:
            index.resolve(term("code:Shared"))
        assert 'code:"' in raised.value.message


class TestErrorPositions:
    def test_it_points_at_the_reference(self, index):
        node = parse('kids AND code:"No such code"')
        assert isinstance(node, And)
        with pytest.raises(KeywordQueryError) as raised:
            for leaf in node.operands:
                if isinstance(leaf, CodeTerm):
                    index.resolve(leaf)
        assert raised.value.position == 9


class TestResolveAll:
    def test_it_passes_a_query_whose_codes_all_place(self, session):
        resolve_all(session, OURS, tree("code:Often AND kids"))

    def test_it_raises_on_the_first_that_does_not(self, session):
        with pytest.raises(KeywordQueryError) as raised:
            resolve_all(session, OURS, tree("code:Often OR code:Nonsense"))
        assert "Nonsense" in raised.value.message

    def test_a_query_with_no_codes_costs_nothing(self, session):
        resolve_all(session, OURS, tree("kids -school"))

    def test_it_reaches_a_code_under_a_negation(self, session):
        with pytest.raises(KeywordQueryError):
            resolve_all(session, OURS, tree("-code:Nonsense"))
