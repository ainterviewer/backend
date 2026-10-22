"""Every error comes back as an `ApiError` body, and the schema says so.

The handlers are mounted on a throwaway app, through the same
`add_error_handlers` the real one uses; the schema checks read the real one.
"""

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.db.repositories.errors import ContentRevisionError, PublishError
from app.errors import AppError, add_error_handlers
from app.main import app as real_app
from app.openapi import build_openapi_schema
from app.types import ErrorCode


@pytest.fixture(scope="module")
def client():
    app = FastAPI()
    add_error_handlers(app)

    @app.get("/app-error")
    def app_error():
        raise AppError("Invite token expired", code=ErrorCode.INVITE_EXPIRED)

    @app.get("/stale")
    def stale():
        raise ContentRevisionError(expected=3, current=5, part="guide")

    @app.get("/cannot-publish")
    def cannot_publish():
        raise PublishError("Translate first", missing={"DA": ["guide.title"]})

    @app.get("/http-exception")
    def http_exception():
        raise HTTPException(404, detail="Interview not found")

    @app.get("/crash")
    def crash():
        raise RuntimeError("boom")

    @app.get("/validated")
    def validated(limit: int):
        return limit

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def test_app_error_carries_its_code(client):
    response = client.get("/app-error")

    assert response.status_code == 400
    assert response.json() == {
        "detail": "Invite token expired",
        "code": "invite_expired",
    }


def test_a_domain_error_brings_its_status_fields_and_headers(client):
    response = client.get("/stale")

    assert response.status_code == 412
    assert response.headers["ETag"] == '"5"'
    assert response.json() == {
        "detail": "The guide changed since it was loaded (revision 3, now 5).",
        "code": "stale_revision",
        "current_revision": 5,
        "part": "guide",
    }


def test_publish_error_lists_what_is_missing(client):
    response = client.get("/cannot-publish")

    assert response.status_code == 409
    assert response.json()["missing"] == {"DA": ["guide.title"]}


def test_a_plain_http_exception_gets_a_null_code(client):
    response = client.get("/http-exception")

    assert response.status_code == 404
    assert response.json() == {"detail": "Interview not found", "code": None}


def test_an_unknown_route_is_an_api_error_too(client):
    assert client.get("/nowhere").json() == {"detail": "Not Found", "code": None}


def test_a_crash_is_json_not_plain_text(client):
    response = client.get("/crash")

    assert response.status_code == 500
    assert response.json()["code"] == "internal"
    assert "boom" not in response.text


def test_validation_errors_keep_fastapis_list(client):
    response = client.get("/validated?limit=many")

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)


@pytest.fixture(scope="module")
def schema():
    return build_openapi_schema(real_app)


class TestSchema:
    def test_every_operation_declares_api_error(self, schema):
        ref = {"$ref": "#/components/schemas/ApiError"}
        for operations in schema["paths"].values():
            for operation in operations.values():
                for status in ("4XX", "5XX"):
                    body = operation["responses"][status]["content"]
                    assert body["application/json"]["schema"] == ref

    def test_validation_errors_are_still_declared(self, schema):
        """A `4XX` on the router would have stopped FastAPI declaring them."""
        login = schema["paths"]["/api/login"]["post"]["responses"]

        assert login["422"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/HTTPValidationError"
        }

    def test_stale_revision_is_declared_where_it_is_raised(self, schema):
        guide = schema["paths"]["/api/projects/{project_id}/guide"]["put"]

        assert guide["responses"]["412"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/StaleRevisionError"
        }

    def test_code_is_always_sent(self, schema):
        assert "code" in schema["components"]["schemas"]["ApiError"]["required"]
