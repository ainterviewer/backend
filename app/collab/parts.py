"""The parts of the content co-edited live, and how each maps to a doc.

A room (`rooms.py`) is generic: it holds a doc, relays it, and saves it
into a column of `projectcontent`. What differs per part is here -- how its
data is written into a doc and read back, and what makes it valid. Every
part's data is JSON; the three-way merge of a write the doc did not make
(`guide_doc._merge`) works on any of it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pycrdt import Doc
from pydantic import BaseModel, ValidationError

from ainterviewer.interview_guides.localized import (
    LocalizedConsent,
    LocalizedInterviewGuide,
    LocalizedWelcome,
)

from ..db.types import ContentPart
from .flat_doc import apply_flat_data, read_flat_data
from .guide_doc import Json, apply_guide_data, read_guide_data

#: A part's value as validated from a doc's data: the model to store, or
#: None for "none configured"; and why it is not valid, if it is not.
type Validated = tuple[BaseModel | None, list[str]]


@dataclass(frozen=True)
class DocPart:
    part: ContentPart
    #: Write data into a doc: `(doc, data, languages, origin)`.
    apply: Callable[[Doc, Json, tuple[str, ...], Any], None]
    read: Callable[[Doc], Json]
    #: `(data, default_language)`.
    validate: Callable[[Json, str], Validated]
    #: The column's value as data; None (nothing configured) as the empty doc.
    dump: Callable[[BaseModel | None], Json]


def _messages(error: ValidationError) -> list[str]:
    return [
        f"{'.'.join(str(part) for part in e['loc'])}: {e['msg']}"
        for e in error.errors()
    ]


# -- The guide -----------------------------------------------------------------


def _validate_guide(data: Json, language: str) -> Validated:
    """Held to what the PUT holds a guide to, conditions included."""
    try:
        guide = LocalizedInterviewGuide.model_validate(data)
    except ValidationError as error:
        return None, _messages(error)
    if order_errors := guide.render(language).condition_order_errors():
        return None, [
            "Conditions must read questions asked before the one they gate: "
            + "; ".join(order_errors)
        ]
    _trim_options(guide)
    return guide, []


def _trim_options(guide: LocalizedInterviewGuide) -> None:
    """Strip stray whitespace from survey options' text, in the guide saved
    rather than in the doc: answers are matched against option text verbatim,
    and trimming the doc would eat the space someone just typed."""
    for section in guide.question_sections:
        for question in section.questions:
            for option in getattr(question.survey_item, "options", None) or []:
                option.text = {lang: text.strip() for lang, text in option.text.items()}


GUIDE = DocPart(
    part=ContentPart.GUIDE,
    apply=lambda doc, data, languages, origin: apply_guide_data(
        doc, data, languages, origin
    ),
    read=read_guide_data,
    validate=_validate_guide,
    dump=lambda value: (value or LocalizedInterviewGuide()).model_dump(mode="json"),
)


# -- Consent and welcome -------------------------------------------------------

_TEXTS = ("title", "text")


def _has_text(data: Json) -> bool:
    return any(text for key in _TEXTS for text in (data.get(key) or {}).values())


def _validate_consent(data: Json, _language: str) -> Validated:
    # No text in any language is no consent at all: respondents then get no
    # consent screen, rather than an empty one.
    if not _has_text(data):
        return None, []
    try:
        return LocalizedConsent.model_validate(data), []
    except ValidationError as error:
        return None, _messages(error)


def _validate_welcome(data: Json, _language: str) -> Validated:
    if (
        not _has_text(data)
        and not data.get("email")
        and not data.get("video_file_name")
    ):
        return None, []
    try:
        return LocalizedWelcome.model_validate(data), []
    except ValidationError as error:
        # Editors see these. The email is the one field the page can leave
        # invalid; say so in its terms rather than pydantic's.
        if any(e["loc"] and e["loc"][0] == "email" for e in error.errors()):
            return None, ["Enter a valid contact email address."]
        return None, _messages(error)


def _flat(part: ContentPart, validate: Callable[[Json, str], Validated]) -> DocPart:
    return DocPart(
        part=part,
        apply=lambda doc, data, languages, origin: apply_flat_data(
            doc, data, _TEXTS, languages, origin
        ),
        read=lambda doc: read_flat_data(doc, _TEXTS),
        validate=validate,
        dump=lambda value: value.model_dump(mode="json") if value is not None else {},
    )


CONSENT = _flat(ContentPart.CONSENT, _validate_consent)
WELCOME = _flat(ContentPart.WELCOME, _validate_welcome)

#: The parts editors co-edit live, by part.
LIVE_PARTS: dict[ContentPart, DocPart] = {
    part.part: part for part in (GUIDE, CONSENT, WELCOME)
}
