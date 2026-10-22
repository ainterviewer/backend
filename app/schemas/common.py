"""Shapes every domain shares: the base model, paging, errors, filters."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import Query
from pydantic import AfterValidator, BaseModel, ConfigDict

from ainterviewer.constants import LANGUAGES
from ainterviewer.types import (
    LanguageCode,
)


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


class PaginatedQueryParams(Schema):
    offset: int = Query(default=0, le=100)
    limit: int = Query(default=20, le=100)
    column: str = Query("created_at", description="Column to sort by")
    order: Literal["asc", "desc"] = Query("desc", description="Sorting order")


class PromptRequest(Schema):
    prompt: str


class PaginatedResponse[T](Schema):
    total: int
    items: list[T]


class ErrorResponse(Schema):
    detail: str


class MediaUploadResponse(Schema):
    message: str
    filename: str
