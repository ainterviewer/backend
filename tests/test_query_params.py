"""Query parameter models, as FastAPI reads them off a request.

Each model is mounted on a throwaway app, because the 422s are FastAPI's and
the only way to see them is through a request.
"""

from typing import Annotated

import pytest
from fastapi import FastAPI, Query
from fastapi.testclient import TestClient

from app.schemas.common import PaginatedQueryParams
from app.schemas.embeddings import MAX_SEARCH_DEPTH, SearchPageParams
from app.schemas.interviews import InterviewFilters


@pytest.fixture(scope="module")
def client():
    app = FastAPI()

    @app.get("/list")
    def listing(
        page: Annotated[PaginatedQueryParams, Query()],
        filters: Annotated[InterviewFilters, Query()],
    ):
        return {
            "page": page.model_dump(),
            "filters": filters.model_dump(exclude_unset=True),
        }

    @app.get("/ranked")
    def ranked(page: Annotated[SearchPageParams, Query()]):
        return page.model_dump()

    with TestClient(app) as client:
        yield client


class TestPaging:
    def test_defaults(self, client):
        page = client.get("/list").json()["page"]

        assert page == {
            "offset": 0,
            "limit": 20,
            "column": "created_at",
            "order": "desc",
        }

    def test_offset_reaches_past_the_first_hundred_rows(self, client):
        """`offset` used to be capped at 100, so the interview list 422'd from
        its seventh page of twenty on."""
        response = client.get("/list?offset=240&limit=20")

        assert response.status_code == 200
        assert response.json()["page"]["offset"] == 240

    @pytest.mark.parametrize("query", ["offset=-1", "limit=0", "limit=101"])
    def test_out_of_range_is_a_422(self, client, query):
        assert client.get(f"/list?{query}").status_code == 422


class TestInterviewFilters:
    def test_read_alongside_paging(self, client):
        """Two parameter models on one route, each reading its own fields."""
        response = client.get(
            "/list?limit=5&statuses=completed&statuses=active&search=dog"
        )

        assert response.status_code == 200
        assert response.json()["page"]["limit"] == 5
        assert response.json()["filters"] == {
            "statuses": ["completed", "active"],
            "search": "dog",
        }

    def test_nothing_given_sets_nothing(self, client):
        assert client.get("/list").json()["filters"] == {}

    def test_search_is_bounded(self, client):
        assert client.get("/list?search=" + "x" * 201).status_code == 422


class TestSearchDepth:
    def test_within_depth(self, client):
        response = client.get(f"/ranked?offset={MAX_SEARCH_DEPTH - 10}&limit=10")

        assert response.status_code == 200

    def test_past_depth_is_a_422(self, client):
        response = client.get(f"/ranked?offset={MAX_SEARCH_DEPTH - 5}&limit=10")

        assert response.status_code == 422
        assert "offset + limit" in response.text
