"""The `responses=` of a route that answers a file rather than JSON.

Declared as a binary string per media type, which the generated client types
as a `Blob`. Without it FastAPI documents an empty JSON body, which the client
types as `unknown`; with a media type but no schema, a `text/*` one (a CSV)
still comes out `unknown`. Pair it with `response_class=Response`, so the JSON
body is not documented alongside.
"""

from typing import Any

_BINARY = {"schema": {"type": "string", "format": "binary"}}


def file_response(*media_types: str) -> dict[int | str, dict[str, Any]]:
    return {200: {"content": {media_type: _BINARY for media_type in media_types}}}
