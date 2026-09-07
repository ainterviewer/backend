"""Tests for the embedding search/cluster filter dependency.

Query-parameter validation only: no database, no inference server. The
dependency is mounted on a throwaway app because that is the only way to
exercise the coercion and the 422s FastAPI does on its behalf.
"""

from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.dashboard.analysis.embeddings import SearchFilterParams


@pytest.fixture(scope="module")
def client():
    app = FastAPI()

    @app.get("/t")
    def endpoint(params: Annotated[SearchFilterParams, Depends()]):
        return {
            "languages": params.filters.languages,
            "questions": params.filters.questions,
        }

    return TestClient(app)


class TestLanguageFilter:
    def test_absent_means_every_language(self, client):
        assert client.get("/t").json()["languages"] is None

    def test_repeatable(self, client):
        """A multilingual project is usually analysed over the languages with
        enough respondents to say anything -- not all of them, not just one."""
        response = client.get("/t?language=DA&language=EN")

        assert response.status_code == 200
        assert response.json()["languages"] == ["DA", "EN"]

    def test_lowercase_is_accepted(self, client):
        """`LanguageCode` carries `to_upper`, and the column stores uppercase,
        so a lowercase code must match rather than silently return nothing."""
        assert client.get("/t?language=da").json()["languages"] == ["DA"]

    def test_malformed_code_is_a_422(self, client):
        """Not a 500. The column type raises ValueError inside the statement,
        which used to surface as a StatementError from deep in the repository.
        """
        response = client.get("/t?language=Danish")

        assert response.status_code == 422

    def test_well_formed_but_unknown_code_is_a_422(self, client):
        """ "DK" is Denmark's country code, not a language. Shape validation
        alone lets it through, and it then matches nothing -- which reads as
        "no Danish data in this project" rather than as a typo."""
        response = client.get("/t?language=DK")

        assert response.status_code == 422
        assert "DK" in response.text


class TestQuestionFilter:
    """`?question=0,2` -- zero-based `section,main_question`, the spelling the
    annotate view already uses, so one filter means the same thing in both
    places."""

    def test_absent_means_every_question(self, client):
        assert client.get("/t").json()["questions"] is None

    def test_repeatable(self, client):
        """A section is asked for by listing its questions, so the common case
        is several pairs at once."""
        response = client.get("/t?question=0,0&question=0,1&question=2,3")

        assert response.status_code == 200
        assert response.json()["questions"] == [[0, 0], [0, 1], [2, 3]]

    def test_duplicates_collapse(self, client):
        """Selecting a section and then one of its questions must not lengthen
        the OR the candidate scan is filtered by."""
        assert client.get("/t?question=1,1&question=1,1").json()["questions"] == [
            [1, 1]
        ]

    def test_wrong_arity_is_a_422(self, client):
        response = client.get("/t?question=0")

        assert response.status_code == 422
        assert "0" in response.text

    def test_non_numeric_is_a_422(self, client):
        """Not a filter that silently matches nothing: a reader cannot tell a
        typo from an empty corpus by looking at the map."""
        response = client.get("/t?question=first,second")

        assert response.status_code == 422
        assert "first,second" in response.text

    def test_negative_index_is_a_422(self, client):
        """Guide coordinates are zero-based and count up; -1 is not "the last
        question", it is a mistake that would match nothing."""
        response = client.get("/t?question=0,-1")

        assert response.status_code == 422
