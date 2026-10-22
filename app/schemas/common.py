"""Shapes every domain shares: the base model, paging, errors, filters."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from ainterviewer.constants import LANGUAGES
from ainterviewer.types import LanguageCode

from ..types import ContentPart, ErrorCode


class Schema(BaseModel):
    """The base of every API schema.

    `from_attributes` so a repository can validate one straight from an ORM
    row. Request bodies inherit it too: it costs nothing on a dict, and one base
    keeps every model's behaviour the same wherever it is declared.
    """

    model_config = ConfigDict(from_attributes=True)


KNOWN_LANGUAGE_CODES = frozenset(entry["code"] for entry in LANGUAGES)


def _known_language(code: str) -> str:
    """Reject a well-formed code that names no language.

    `LanguageCode` only checks the shape, which lets a plausible wrong answer
    through: "DK" is Denmark's country code and not a language, so filtering on
    it matches nothing and looks like "this project has no Danish data" rather
    than like a typo.
    """
    if code not in KNOWN_LANGUAGE_CODES:
        raise ValueError(f"Unknown language code: {code}")
    return code


#: A language code as a *filter* value, validated against the known set so a
#: bad one is a 422 rather than an empty result set or a 500 from the column
#: type. Uppercased on the way in, since `LanguageCode` carries `to_upper`.
LanguageFilter = Annotated[LanguageCode, AfterValidator(_known_language)]


class OffsetParams(Schema):
    """`limit`/`offset` paging, as query parameters, for a list whose order
    the server fixes. Taken as `Annotated[OffsetParams, Query()]`."""

    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)


class PageParams(OffsetParams):
    """`limit`/`offset` paging with a sort the caller chooses."""

    column: str = Field(default="created_at", description="Column to sort by")
    order: Literal["asc", "desc"] = Field(default="desc", description="Sorting order")


class PromptRequest(Schema):
    prompt: str


class PaginatedResponse[T](Schema):
    total: int
    items: list[T]


class ActionResponse(Schema):
    """The result of an action that returns nothing but a sentence to show
    (logged out, password reset). Errors are `ApiError`s instead."""

    message: str


class ApiError(Schema):
    """The body of every error response but FastAPI's 422, whose `detail` is
    a list of validation errors (`HTTPValidationError`).

    `detail` is a sentence to show; `code` says what went wrong for a client
    that handles it, and is null for errors no client tells apart. Raised as
    `app.errors.AppError` and answered by `app.errors.add_error_handlers`.
    """

    detail: str
    code: ErrorCode | None


class StaleRevisionError(ApiError):
    """412 `stale_revision`: someone saved the content since it was loaded.

    `current_revision` is what to send as `If-Match` to overwrite; `part` the
    `ContentPart` whose revision was checked, null for the overall one.
    """

    current_revision: int
    part: ContentPart | None


class CannotPublishError(ApiError):
    """409 `cannot_publish`: the paths each offered language still lacks."""

    missing: dict[LanguageCode, list[str]]


class InvalidKeywordQueryError(ApiError):
    """400 `invalid_keyword_query`: where in the query it went wrong, as a
    zero-based offset, or null when the problem is the query as a whole."""

    position: int | None


class MediaUploadResponse(Schema):
    message: str
    filename: str
