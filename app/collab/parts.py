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
from pathlib import PureWindowsPath
from typing import Any
from uuid import UUID

from pycrdt import Doc
from pydantic import BaseModel, ValidationError

from ainterviewer.interview_guides import GuideError, validation_errors
from ainterviewer.interview_guides.extra import EMAIL_REQUIRED
from ainterviewer.interview_guides.localized import (
    LocalizedConsent,
    LocalizedInterviewGuide,
    LocalizedWelcome,
)

from ..types import ContentError, ContentPart
from .flat_doc import apply_flat_data, read_flat_data
from .guide_doc import Json, apply_guide_data, read_guide_data

#: A part's value as validated from a doc's data: the model to store, or
#: None for "none configured"; and why it is not valid, if it is not.
type Validated = tuple[BaseModel | None, list[ContentError]]


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


def _messages(error: ValidationError) -> list[ContentError]:
    return [
        ContentError(
            message=f"{'.'.join(str(part) for part in e['loc'])}: {e['msg']}",
            field=str(e["loc"][0]) if e["loc"] else None,
        )
        for e in error.errors()
    ]


# -- The guide -----------------------------------------------------------------


def _validate_guide(data: Json, language: str) -> Validated:
    """Held to what the PUT holds a guide to, conditions included -- except
    that questions with no text yet are left out rather than refused, so a
    guide can be laid out first and its questions written one at a time,
    each tried as it is. They still hold back publishing, as `skipped`
    errors. What reads one of them (a condition, a reference) cannot do
    without it, and is refused."""
    try:
        guide = LocalizedInterviewGuide.model_validate(data)
    except ValidationError as error:
        return None, [ContentError.of(e) for e in validation_errors(error, data)]
    empty = guide.empty_question_errors()
    skipped = {error.question_id for error in empty}
    errors = [
        *(ContentError.of(e) for e in image_name_errors(guide)),
        *(ContentError.of(e) for e in guide.render(language).condition_order_errors()),
        *_reads_skipped(guide, skipped),
    ]
    if errors:
        return None, [*errors, *(ContentError.of(e, skipped=True) for e in empty)]
    _trim_options(guide)
    if skipped:
        guide = _without_questions(guide, skipped)
    return guide, [ContentError.of(e, skipped=True) for e in empty]


def image_name_errors(guide: LocalizedInterviewGuide) -> list[GuideError]:
    """Images named by anything but a bare file name.

    An image's name is joined onto the project's image directory, to write
    an uploaded one and to read it for respondents, so a name carrying a
    path ("../../...") would reach files outside it. Names the upload route
    hands out are always bare; anything else was written by hand. Checked
    on save, like `empty_question_errors`, so guides stored before still
    load.
    """
    return [
        GuideError(
            message=f"Question {n_section + 1}.{n_question + 1} names its image "
            f"{question.image.name!r}, which is not a file name.",
            fix="Upload the image again.",
            question_id=question.id,
            field="image",
        )
        for n_section, section in enumerate(guide.question_sections)
        for n_question, question in enumerate(section.questions)
        if question.image is not None and not _is_bare_name(question.image.name)
    ]


def _is_bare_name(name: str) -> bool:
    return (
        name not in {"", ".", ".."}
        and "\x00" not in name
        # Windows paths split on both "/" and "\\".
        and PureWindowsPath(name).name == name
    )


def _reads_skipped(
    guide: LocalizedInterviewGuide, skipped: set[UUID | None]
) -> list[ContentError]:
    """Conditions and references of the questions kept that read one left
    out: without it, they would read nothing."""
    numbers = {
        question.id: f"{n_section + 1}.{n_question + 1}"
        for n_section, section in enumerate(guide.question_sections)
        for n_question, question in enumerate(section.questions)
    }
    errors = []
    for section in guide.question_sections:
        for question in section.questions:
            if question.id in skipped:
                continue
            reads = [
                (
                    "conditions",
                    "has a condition on",
                    "change the condition",
                    block.question_context.question_id,
                )
                for condition in question.conditions
                for block in condition.blocks
            ] + [
                (
                    "references",
                    "refers to the answer to",
                    "remove the reference",
                    reference.question_id,
                )
                for reference in question.references or []
            ]
            seen = set()
            for field, does, undo, read in reads:
                if read not in skipped or (field, read) in seen:
                    continue
                seen.add((field, read))
                errors.append(
                    ContentError(
                        message=f"Question {numbers[question.id]} {does} question "
                        f"{numbers[read]}, which has no text yet.",
                        fix=f"Write question {numbers[read]}, or {undo}.",
                        question_id=question.id,
                        field=field,
                    )
                )
    return errors


def _without_questions(
    guide: LocalizedInterviewGuide, question_ids: set[UUID | None]
) -> LocalizedInterviewGuide:
    """`guide` without the questions `question_ids`, validated again so that
    what reads a question by position follows the ones moved up."""
    data = guide.model_dump(mode="json")
    ids = {str(question_id) for question_id in question_ids}
    for section in data["question_sections"]:
        section["questions"] = [q for q in section["questions"] if q["id"] not in ids]
    return LocalizedInterviewGuide.model_validate(data)


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
    # Only the contact box shows the email: while it is hidden, what the doc
    # holds there is kept for when it is shown again, but neither saved nor
    # checked.
    if data.get("with_id") is False:
        data = {**data, "email": None}
    if (
        not _has_text(data)
        and not data.get("email")
        and not data.get("video_file_name")
    ):
        return None, []
    try:
        return LocalizedWelcome.model_validate(data), []
    except ValidationError as error:
        if (email_error := _email_error(error)) is None:
            return None, _messages(error)
    # The email is the one field the page can leave invalid. Saved without
    # the contact box, which shows nothing without it -- the interview ID is
    # of no use to a respondent with no one to send it to -- so tests run on
    # the rest of the page; publishing waits on it (`skipped`).
    try:
        hidden = LocalizedWelcome.model_validate(
            {**data, "email": None, "with_id": False}
        )
    except ValidationError:
        return None, [email_error]
    return hidden, [email_error.model_copy(update={"skipped": True})]


def _email_error(error: ValidationError) -> ContentError | None:
    """The welcome's email problem, in the page's terms rather than
    pydantic's: not an email, or none while the contact box shows."""
    if any(e["loc"] and e["loc"][0] == "email" for e in error.errors()):
        return ContentError(
            message="Enter a valid contact email address.", field="email"
        )
    if any(EMAIL_REQUIRED in e["msg"] for e in error.errors()):
        return ContentError(
            message="Enter a contact email address.",
            fix="Or stop showing the contact box.",
            field="email",
        )
    return None


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
