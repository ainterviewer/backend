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
)
from ainterviewer.lpm.types import CustomToken

from .api.dashboard.assistance import ChatMessage
from .auth import AuthToken, InterviewToken
from .utils import extend_openapi_schema

# Models the frontend consumes over the WebSocket or out of tokens, which are
# unreachable from any HTTP route and so absent from the generated schema.
EXTRA_MODELS = [
    AuthToken,
    ChatMessage,
    CustomToken,
    InterviewToken,
    OutgoingData,
    OutgoingHistoryMessage,
    OutgoingMessage,
    ReceivedData,
]


def build_openapi_schema(app: FastAPI) -> dict[str, Any]:
    """Build the SDK-facing OpenAPI schema for `app`."""
    openapi = app.openapi()

    openapi["paths"] = {
        path: spec
        for path, spec in openapi["paths"].items()
        if path.startswith(("/api/", "/ws/"))
    }

    # TODO: Make sure that we should in fact extend the openapi schema, and not
    # just export them as separately
    return extend_openapi_schema(openapi, models=EXTRA_MODELS)
