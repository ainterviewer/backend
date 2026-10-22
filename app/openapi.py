"""Construction of the OpenAPI schema the frontend SDK is generated from.

This is deliberately *not* the schema FastAPI serves at `/openapi.json`: the
frontend needs a handful of models that no HTTP route references (the WebSocket
message types and token payloads), and it has no use for non-API routes. Both
the `/api/openapi.json` endpoint and the `generate-openapi-scheme` CLI command
go through here so the two can never drift apart.
"""

from typing import Any

from fastapi import FastAPI

from ainterviewer.interfaces import (
    OutgoingData,
    OutgoingHistoryMessage,
    OutgoingMessage,
    ReceivedData,
    SecurityOverride,
)
from ainterviewer.lpm.types import CustomToken

from .api.dashboard.assistance import ChatMessage
from .auth import AuthToken, InterviewToken
from .schemas.common import ApiError
from .services.email.participant_template import TemplatePlaceholder
from .utils import extend_openapi_schema

# Models the frontend consumes over the WebSocket or out of tokens, which are
# unreachable from any HTTP route and so absent from the generated schema.
# `TemplatePlaceholder` is here for a different reason: no route takes or
# returns it, but the dashboard's email editor needs the authoritative list of
# template variables, and exporting it keeps that list in one place.
EXTRA_MODELS = [
    ApiError,
    AuthToken,
    ChatMessage,
    CustomToken,
    InterviewToken,
    OutgoingData,
    OutgoingHistoryMessage,
    OutgoingMessage,
    ReceivedData,
    SecurityOverride,
    TemplatePlaceholder,
]


def build_openapi_schema(app: FastAPI) -> dict[str, Any]:
    """Build the SDK-facing OpenAPI schema for `app`."""
    openapi = app.openapi()

    openapi["paths"] = {
        path: spec
        for path, spec in openapi["paths"].items()
        if path.startswith(("/api/", "/ws/"))
    }

    _declare_errors(openapi)

    # TODO: Make sure that we should in fact extend the openapi schema, and not
    # just export them as separately
    return extend_openapi_schema(openapi, models=EXTRA_MODELS)


#: Every error the handlers in app/main.py answer, but FastAPI's own 422.
_API_ERROR = {
    "content": {
        "application/json": {"schema": {"$ref": "#/components/schemas/ApiError"}}
    }
}


def _declare_errors(openapi: dict[str, Any]) -> None:
    """Declare `ApiError` as every operation's 4XX and 5XX body, so the
    generated client types each one's `error`.

    Added here rather than as `responses` on the API router because FastAPI
    stops declaring its own 422 on an operation that has a `4XX`, and that
    body, a list of validation errors, is not an `ApiError`. A route whose
    error carries more fields (`StaleRevisionError`) declares it for that
    status itself, which a range does not override.
    """
    for operations in openapi["paths"].values():
        for operation in operations.values():
            responses = operation.setdefault("responses", {})
            responses.setdefault("4XX", {"description": "Client error", **_API_ERROR})
            responses.setdefault("5XX", {"description": "Server error", **_API_ERROR})
