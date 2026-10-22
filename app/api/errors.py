"""Error bodies that carry more than an `ApiError`, for the routes that answer
them to declare in `responses=`.

Every operation already declares `ApiError` for 4XX and 5XX (`app/openapi.py`);
a status declared here takes precedence for that route. Where other errors
share the status, the model is a union with `ApiError`, so the client narrows
on `code` rather than trusting a field to be there.
"""

from typing import Any

from ..schemas.common import (
    ApiError,
    CannotPublishError,
    InvalidKeywordQueryError,
    StaleRevisionError,
)

#: A content write whose `If-Match` is behind (`ContentRevisionError`).
STALE_REVISION: dict[int | str, dict[str, Any]] = {
    412: {"model": StaleRevisionError, "description": "Stale revision"}
}

#: A publish refused while an offered language lacks text (`PublishError`).
CANNOT_PUBLISH: dict[int | str, dict[str, Any]] = {
    409: {"model": CannotPublishError | ApiError, "description": "Conflict"}
}

#: A keyword filter that does not parse or names no code (`KeywordQueryError`).
INVALID_KEYWORD_QUERY: dict[int | str, dict[str, Any]] = {
    400: {"model": InvalidKeywordQueryError | ApiError, "description": "Bad Request"}
}
