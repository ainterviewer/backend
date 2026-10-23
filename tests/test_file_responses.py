"""Routes that answer a file declare it as binary, per media type.

Without that FastAPI documents an empty JSON body, and the generated client
types the download as `unknown` -- every caller then casts it to a `Blob`.
"""

import pytest

from app.main import app
from app.openapi import build_openapi_schema

BINARY = {"type": "string", "format": "binary"}

FILE_ROUTES = [
    ("get", "/api/experiments/{experiment_id}/qr.png"),
    ("get", "/api/projects/{project_id}/qr.png"),
    ("get", "/api/projects/{project_id}/interviews/{interview_id}/audio/{filename}"),
    ("post", "/api/projects/{project_id}/interviews/messages/export"),
    ("post", "/api/projects/{project_id}/participants/export"),
    ("get", "/api/projects/{project_id}/email-bundle"),
    ("post", "/api/speech"),
]


@pytest.fixture(scope="module")
def schema():
    return build_openapi_schema(app)


@pytest.mark.parametrize(("method", "path"), FILE_ROUTES)
def test_a_file_route_declares_only_binary_content(schema, method, path):
    content = schema["paths"][path][method]["responses"]["200"]["content"]

    assert "application/json" not in content
    assert content and all(entry["schema"] == BINARY for entry in content.values())
