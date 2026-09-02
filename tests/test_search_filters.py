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
        return {"languages": params.filters.languages}

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
