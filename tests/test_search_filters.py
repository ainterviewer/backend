"""Tests for the embedding search/cluster filter dependency.

Parameter validation and the 422s FastAPI does on its behalf. The dependency is
mounted on a throwaway app because that is the only way to exercise them.

It takes a project and a session as well as query parameters, and has since
`code:` joined the keyword grammar: placing a code name in a codebook is the
half of reading a query that cannot be done without the project. So the
throwaway app carries a project id in its path and a real -- if nearly empty --
database behind it. Everything that is not a code reference still never touches
that database.
"""

import uuid
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.dashboard.analysis.embeddings import SearchFilterParams
from app.db.crud import InterviewDataBase
from app.db.tables import (
    Base,
    CodeTable,
    ProjectFolderTable,
    ProjectTable,
)
from app.db.types import CodeKind
from app.dependencies import get_db

FOLDER = uuid.uuid4()
PROJECT = uuid.uuid4()
STRESS = uuid.uuid4()
OFTEN = uuid.uuid4()
THEMES = uuid.uuid4()
COST = uuid.uuid4()


@pytest.fixture(scope="module")
def client():
    # `TestClient` runs the app on another thread, and an in-memory SQLite
    # connection belongs to the thread that opened it -- so one shared
    # connection, explicitly allowed to cross.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(ProjectFolderTable(id=FOLDER, title="Folder"))
    session.add(
        ProjectTable(
            id=PROJECT, folder_id=FOLDER, title="Project", owner_id=uuid.uuid4()
        )
    )
    session.add(
        CodeTable(id=STRESS, project_id=PROJECT, name="Stress", kind=CodeKind.TAG)
    )
    session.add(
        CodeTable(
            id=OFTEN,
            project_id=PROJECT,
            parent_id=STRESS,
            name="Often",
            kind=CodeKind.TAG,
        )
    )
    session.add(
        CodeTable(id=THEMES, project_id=PROJECT, name="Themes", kind=CodeKind.GROUP)
    )
    session.add(
        CodeTable(
            id=COST,
            project_id=PROJECT,
            parent_id=THEMES,
            name="Cost",
            kind=CodeKind.TAG,
        )
    )
    session.commit()

    app = FastAPI()

    @app.get("/t/{project_id}")
    def endpoint(params: Annotated[SearchFilterParams, Depends()]):
        return {
            "languages": params.filters.languages,
            "questions": params.filters.questions,
            "keyword": params.filters.keyword,
        }

    app.dependency_overrides[get_db] = lambda: InterviewDataBase(session)
    with TestClient(app) as client:
        yield client
    session.close()


@pytest.fixture(scope="module")
def path():
    return f"/t/{PROJECT}"


class TestLanguageFilter:
    def test_absent_means_every_language(self, client, path):
        assert client.get(path).json()["languages"] is None

    def test_repeatable(self, client, path):
        """A multilingual project is usually analysed over the languages with
        enough respondents to say anything -- not all of them, not just one."""
        response = client.get(path + "?language=DA&language=EN")

        assert response.status_code == 200
        assert response.json()["languages"] == ["DA", "EN"]

    def test_lowercase_is_accepted(self, client, path):
        """`LanguageCode` carries `to_upper`, and the column stores uppercase,
        so a lowercase code must match rather than silently return nothing."""
        assert client.get(path + "?language=da").json()["languages"] == ["DA"]

    def test_malformed_code_is_a_422(self, client, path):
        """Not a 500. The column type raises ValueError inside the statement,
        which used to surface as a StatementError from deep in the repository.
        """
        response = client.get(path + "?language=Danish")

        assert response.status_code == 422

    def test_well_formed_but_unknown_code_is_a_422(self, client, path):
        """ "DK" is Denmark's country code, not a language. Shape validation
        alone lets it through, and it then matches nothing -- which reads as
        "no Danish data in this project" rather than as a typo."""
        response = client.get(path + "?language=DK")

        assert response.status_code == 422
        assert "DK" in response.text


class TestQuestionFilter:
    """`?question=0,2` -- zero-based `section,main_question`, the spelling the
    annotate view already uses, so one filter means the same thing in both
    places."""

    def test_absent_means_every_question(self, client, path):
        assert client.get(path).json()["questions"] is None

    def test_repeatable(self, client, path):
        """A section is asked for by listing its questions, so the common case
        is several pairs at once."""
        response = client.get(path + "?question=0,0&question=0,1&question=2,3")

        assert response.status_code == 200
        assert response.json()["questions"] == [[0, 0], [0, 1], [2, 3]]

    def test_duplicates_collapse(self, client, path):
        """Selecting a section and then one of its questions must not lengthen
        the OR the candidate scan is filtered by."""
        assert client.get(path + "?question=1,1&question=1,1").json()["questions"] == [
            [1, 1]
        ]

    def test_wrong_arity_is_a_422(self, client, path):
        response = client.get(path + "?question=0")

        assert response.status_code == 422
        assert "0" in response.text

    def test_non_numeric_is_a_422(self, client, path):
        """Not a filter that silently matches nothing: a reader cannot tell a
        typo from an empty corpus by looking at the map."""
        response = client.get(path + "?question=first,second")

        assert response.status_code == 422
        assert "first,second" in response.text

    def test_negative_index_is_a_422(self, client, path):
        """Guide coordinates are zero-based and count up; -1 is not "the last
        question", it is a mistake that would match nothing."""
        response = client.get(path + "?question=0,-1")

        assert response.status_code == 422


class TestCodeReferences:
    """`code:` in the keyword box, which is the one filter that needs the
    project to be read at all."""

    def test_a_name_that_places_is_accepted(self, client, path):
        response = client.get(path + "?keyword=code:Often")

        assert response.status_code == 200
        assert response.json()["keyword"] == "code:Often"

    def test_a_subtree_is_accepted(self, client, path):
        assert client.get(path + "?keyword=code:Stress/*").status_code == 200

    def test_it_composes_with_words(self, client, path):
        assert client.get(path + "?keyword=code:Often AND kids").status_code == 200

    def test_a_name_nobody_can_place_is_a_422(self, client, path):
        """Not an empty result. A filter that quietly matched nothing would read
        as "nobody was coded that way" rather than as "there is no such code"."""
        response = client.get(path + "?keyword=code:Nonsense")

        assert response.status_code == 422
        assert "Nonsense" in response.text

    def test_the_422_carries_a_position(self, client, path):
        response = client.get(path + "?keyword=kids AND code:Nonsense")

        assert response.status_code == 422
        assert response.json()["detail"]["position"] == 9

    def test_a_group_asked_for_exactly_is_a_422(self, client, path):
        """Nothing is ever coded with a GROUP, so the query cannot match -- and
        the reader almost certainly meant the branch under it."""
        response = client.get(path + "?keyword=code:Themes")

        assert response.status_code == 422
        assert "/*" in response.text

    def test_the_same_group_with_a_star_is_fine(self, client, path):
        assert client.get(path + "?keyword=code:Themes/*").status_code == 200

    def test_a_malformed_reference_is_still_a_422(self, client, path):
        response = client.get(path + "?keyword=code:")

        assert response.status_code == 422
