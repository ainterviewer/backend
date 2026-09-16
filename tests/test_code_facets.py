"""Turning chunk sets into the numbers beside a code row.

The SQL that finds the chunks is `test_browse.py::TestCodeFacets`; this is the
arithmetic on top of it, which is one rule worth pinning on its own: a branch
is the *union* of what its codes reached, never the sum.
"""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ainterviewer.types import EmbeddingKind
from app.api.dashboard.analysis.embeddings import code_facets
from app.db.code_lookup import CodeIndex
from app.db.repositories.embedding import CodeCoverage
from app.db.tables import Base, CodeTable
from app.db.types import CodeKind

PROJECT = uuid.uuid4()
WELLBEING = uuid.uuid4()
STRAIN = uuid.uuid4()
FATIGUE = uuid.uuid4()
COMMUTE = uuid.uuid4()


@pytest.fixture
def index():
    """One branch of two codes under a group, and one code outside it."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add_all(
        [
            CodeTable(
                id=WELLBEING, project_id=PROJECT, name="Wellbeing", kind=CodeKind.GROUP
            ),
            CodeTable(
                id=STRAIN,
                project_id=PROJECT,
                parent_id=WELLBEING,
                name="Strain",
                kind=CodeKind.TAG,
            ),
            CodeTable(
                id=FATIGUE,
                project_id=PROJECT,
                parent_id=WELLBEING,
                name="Fatigue",
                kind=CodeKind.TAG,
            ),
            CodeTable(
                id=COMMUTE, project_id=PROJECT, name="Commute", kind=CodeKind.TAG
            ),
        ]
    )
    session.flush()
    return CodeIndex.for_project(session, PROJECT)


def facets(index, units, total=10):
    found = code_facets(
        index, CodeCoverage(total=total, units=units), EmbeddingKind.QA_PAIR
    )
    return {item.code_id: (item.count, item.subtree) for item in found.items}


class TestCodeFacets:
    def test_a_leaf_counts_itself_twice_over(self, index):
        """Own and subtree agree on a code with nothing under it, which is
        what lets the panel show one number for most rows."""
        assert facets(index, {COMMUTE: {("a",)}})[COMMUTE] == (1, 1)

    def test_a_group_carries_only_its_branch(self, index):
        found = facets(index, {STRAIN: {("a",)}, FATIGUE: {("b",)}})
        assert found[WELLBEING] == (0, 2)

    def test_a_branch_is_a_union_and_not_a_sum(self, index):
        """The same chunk coded with both children. Summed it would read 2,
        which is more chunks than the two codings sit in."""
        found = facets(index, {STRAIN: {("a",)}, FATIGUE: {("a",)}})
        assert found[WELLBEING] == (0, 1)

    def test_a_parent_and_its_child_on_one_chunk_is_one_chunk(self, index):
        # A TAG with children: its own count is what it alone reached, its
        # subtree the branch including itself.
        found = facets(index, {WELLBEING: {("a",)}, STRAIN: {("a",)}})
        assert found[WELLBEING] == (1, 1)

    def test_codes_nothing_reached_are_left_out(self, index):
        found = facets(index, {STRAIN: {("a",)}})
        assert COMMUTE not in found
        assert set(found) == {STRAIN, WELLBEING}

    def test_an_empty_coverage_is_an_empty_list(self, index):
        assert facets(index, {}) == {}

    def test_the_total_comes_through(self, index):
        found = code_facets(
            index, CodeCoverage(total=7, units={}), EmbeddingKind.SECTION
        )
        assert (found.total, found.kind) == (7, EmbeddingKind.SECTION)
