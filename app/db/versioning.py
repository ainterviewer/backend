"""The semantic version a publish gets: how far the new content is from the
last published one, judged by whether interviews remain comparable.

- **Major**: something interviews already answered is gone or reshaped -- a
  section or question removed, a survey item's type, options (one removed) or
  scale changed, or a question's conditions changed. Answers from either side
  of a major bump may not line up.
- **Minor**: something was added or a setting changed -- a section, question
  or option added, items reordered or moved, probing and other question
  settings, agent configs, prompt overrides, the languages offered. Every
  question earlier interviews answered is still there, asked the same way.
- **Patch**: text only -- wording, translations, consent, welcome, emails.

So every interview within one major version answered the same questions,
structured the same way. Only the wording is beyond the diff's reach: a
rewording that changes what a question means is a patch to it, which is why
whoever publishes may raise the bump (never lower it).

Everything is matched by the stable ids sections, questions and options carry,
never by position.
"""

from enum import IntEnum
from typing import Any, Literal

from pydantic import BaseModel
from pydantic_core import to_jsonable_python

from ainterviewer.interview_guides import LocalizedInterviewGuide
from ainterviewer.interview_guides.localized import LocalizedQuestion

from .tables import ContentColumns

BumpName = Literal["none", "patch", "minor", "major"]


class Bump(IntEnum):
    NONE = 0
    PATCH = 1
    MINOR = 2
    MAJOR = 3

    @property
    def label(self) -> BumpName:
        return _BUMP_NAMES[self]

    @classmethod
    def named(cls, name: BumpName) -> "Bump":
        return cls[name.upper()]


_BUMP_NAMES: dict[Bump, BumpName] = {
    Bump.NONE: "none",
    Bump.PATCH: "patch",
    Bump.MINOR: "minor",
    Bump.MAJOR: "major",
}

SemVer = tuple[int, int, int]

#: What a project's first publish is called.
FIRST_VERSION: SemVer = (1, 0, 0)


def bumped(version: SemVer, bump: Bump) -> SemVer:
    major, minor, patch = version
    match bump:
        case Bump.MAJOR:
            return (major + 1, 0, 0)
        case Bump.MINOR:
            return (major, minor + 1, 0)
        case Bump.PATCH:
            return (major, minor, patch + 1)
        case Bump.NONE:
            return version


def format_semver(version: SemVer) -> str:
    return ".".join(map(str, version))


def _dump(value: Any) -> Any:
    return to_jsonable_python(value)


def _without_text(dumped: Any) -> Any:
    """`dumped` with every text leaf dropped: a `{language: text}` map whose
    keys are language codes becomes None, so writing a first translation of an
    optional field does not read as a structural change."""
    if isinstance(dumped, dict):
        if dumped and all(
            isinstance(k, str) and len(k) == 2 and k.isupper() for k in dumped
        ):
            return None
        if not dumped:
            return None
        return {k: _without_text(v) for k, v in dumped.items()}
    if isinstance(dumped, list):
        return [_without_text(v) for v in dumped]
    return dumped


def _structure(model: BaseModel | None, exclude: set[str] | None = None) -> Any:
    if model is None:
        return None
    return _without_text(_dump(model.model_dump(mode="json", exclude=exclude)))


def _survey_bump(old: LocalizedQuestion, new: LocalizedQuestion) -> Bump:
    before, after = old.survey_item, new.survey_item
    if before is None and after is None:
        return Bump.NONE
    if before is None or after is None or before.type != after.type:
        return Bump.MAJOR
    old_options = {o.id for o in getattr(before, "options", None) or []}
    new_options = {o.id for o in getattr(after, "options", None) or []}
    if old_options - new_options:
        return Bump.MAJOR
    rest_before = _structure(before, exclude={"options"})
    rest_after = _structure(after, exclude={"options"})
    if rest_before != rest_after:
        # The scale, the slider's range, "other" allowed: answers read
        # differently on either side.
        return Bump.MAJOR
    old_order = [o.id for o in getattr(before, "options", None) or []]
    new_order = [o.id for o in getattr(after, "options", None) or []]
    if old_order != new_order:
        return Bump.MINOR  # added, or reordered
    return Bump.NONE


def _question_bump(old: LocalizedQuestion, new: LocalizedQuestion) -> Bump:
    bump = _survey_bump(old, new)
    if _structure(old.conditions) != _structure(new.conditions):
        bump = max(bump, Bump.MAJOR)
    settings = {"survey_item", "conditions"}
    if _structure(old, exclude=settings) != _structure(new, exclude=settings):
        bump = max(bump, Bump.MINOR)
    return bump


def _guide_bump(old: LocalizedInterviewGuide, new: LocalizedInterviewGuide) -> Bump:
    old_sections = {s.id: s for s in old.question_sections}
    new_sections = {s.id: s for s in new.question_sections}
    old_questions = {q.id: q for s in old.question_sections for q in s.questions}
    new_questions = {q.id: q for s in new.question_sections for q in s.questions}

    if old_sections.keys() - new_sections.keys():
        return Bump.MAJOR
    if old_questions.keys() - new_questions.keys():
        return Bump.MAJOR

    bump = Bump.NONE
    for question_id, question in old_questions.items():
        bump = max(bump, _question_bump(question, new_questions[question_id]))
    if bump == Bump.MAJOR:
        return bump

    if new_sections.keys() - old_sections.keys():
        bump = max(bump, Bump.MINOR)
    if new_questions.keys() - old_questions.keys():
        bump = max(bump, Bump.MINOR)

    # Order and placement: where each question sits, and the sections' order.
    def layout(guide: LocalizedInterviewGuide):
        return [(s.id, [q.id for q in s.questions]) for s in guide.question_sections]

    if layout(old) != layout(new):
        bump = max(bump, Bump.MINOR)

    # Section and guide-wide settings.
    for section_id, section in old_sections.items():
        if _structure(section, exclude={"questions"}) != _structure(
            new_sections[section_id], exclude={"questions"}
        ):
            bump = max(bump, Bump.MINOR)
    if _structure(old, exclude={"question_sections"}) != _structure(
        new, exclude={"question_sections"}
    ):
        bump = max(bump, Bump.MINOR)
    return bump


def classify(old: ContentColumns, new: ContentColumns) -> Bump:
    """How far `new` is from `old`; NONE when the content is the same."""
    guide = _guide_bump(old.guide, new.guide)
    if guide == Bump.MAJOR:
        return guide
    bump = guide

    if (
        list(old.languages) != list(new.languages)
        or old.default_language != new.default_language
        or _dump(old.agent_configs) != _dump(new.agent_configs)
        or dict(old.prompt_overrides) != dict(new.prompt_overrides)
    ):
        bump = max(bump, Bump.MINOR)

    if bump == Bump.NONE and any(
        _dump(getattr(old, field)) != _dump(getattr(new, field))
        for field in ContentColumns.CONTENT_FIELDS
    ):
        # Whatever is left is text: wording, translations, consent, welcome,
        # emails.
        bump = Bump.PATCH
    return bump
