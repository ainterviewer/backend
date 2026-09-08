"""Reading a cohort's survey answers, and filtering interviews by them.

An interview's survey answers are what its respondent *is* rather than what
they said: an age, a gender, a satisfaction rating. The explore view filters by
them for that reason -- "show me what the dissatisfied respondents talked
about" is a cohort question, and it narrows every chunk of every matching
interview rather than picking out the answer messages themselves.

The answers live in the message table like everything else: a survey item is a
snapshot on the *question* message, and the answer is the respondent message
that follows it. That pairing is the one thing this module knows that nothing
else does, and it is why both the filter and the picker behind it read from
here rather than each finding their own way to the rows.

Values are identified by *position* in the option list, not by text. The same
item asked in two languages offers the same choices translated -- "Female" and
"Kvinde" are one answer -- so filtering by text would quietly drop every
respondent who was interviewed in the other language. The report page groups
its bars by position for exactly this reason, and a filter that disagreed with
the chart it is read beside would be worse than no filter.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple
from uuid import UUID

from sqlalchemy import Text, and_, cast, func, or_, select
from sqlalchemy.orm import Session, aliased

from ainterviewer.interview_guides import SurveyItem
from ainterviewer.interview_guides.survey_items import (
    CheckboxItem,
    DateItem,
    DatetimeItem,
    LikertItem,
    NumberItem,
    RadioItem,
    SliderItem,
    TimeItem,
)
from ainterviewer.lpm.types import CustomToken
from ainterviewer.types import MessageRole, MessageType

from .tables import InterviewTable, MessageTable
from .types import InterviewType

#: A place in the interview guide: zero-based ``(section, main_question)``, the
#: same pair the question filter and the annotate view use.
Coordinate = tuple[int, int]

#: What the respondent's client submits for a question they declined. Stored
#: verbatim as the answer's content, so it has to be read as "no answer" rather
#: than as a value somebody chose.
SKIP_TOKENS = frozenset(token.value for token in CustomToken)

#: Item types whose answers are one of a fixed list of options.
CATEGORICAL_TYPES = (RadioItem, LikertItem, CheckboxItem)
#: Item types whose answers are numbers, filtered by range rather than by value.
NUMERIC_TYPES = (SliderItem, NumberItem)
#: Item types whose answers are points in time, also filtered by range.
TEMPORAL_TYPES = (DateItem, DatetimeItem, TimeItem)


def has_survey_item(column):
    """Whether a message carries a survey item.

    Not ``column.is_(None)``, which never matches: ``PydanticJSONB`` writes
    JSON ``null`` rather than SQL NULL, so every row is non-NULL and the
    obvious test silently selects nothing. Checked as text because that is the
    one reading both a JSON null and a real object honestly.
    """
    return and_(column.is_not(None), cast(column, Text) != "null")


def normalize_answer(value: str) -> str:
    """The form two answers are compared on.

    An answer arrives as the text the respondent's client submitted, which for
    a conversational interview is not always a verbatim copy of the option:
    ``"yes"`` and ``"Yes"`` are the same choice and have to compare equal.
    """
    return " ".join(value.split()).casefold()


def split_checkbox(answer: str) -> list[str]:
    """Undo the ``", "`` join the checkbox widget submits its selection with."""
    return [part.strip() for part in answer.split(",") if part.strip()]


def index_options(options: Sequence[str]) -> dict[str, int]:
    """Map each option's normalized form to its position. First spelling wins."""
    index: dict[str, int] = {}
    for position, option in enumerate(options):
        index.setdefault(normalize_answer(option), position)
    return index


def options_of(item: SurveyItem | None) -> list[str] | None:
    """The item's authored options, or None where it has none."""
    return getattr(item, "options", None)


class AnswerRow(NamedTuple):
    """One respondent's answer to one survey item.

    `item` is the snapshot taken off the question message, so its options are
    the ones this respondent actually saw, in the language their interview ran
    in. That is what makes position mean the same thing across languages.
    """

    interview_id: UUID
    coordinate: Coordinate
    content: str
    item: SurveyItem | None
    language: str


class OptionValue(NamedTuple):
    """An answer that is one of the item's options, named by its position."""

    index: int


class TextValue(NamedTuple):
    """An answer that is not one of the options: typed into an "Other" field,
    or left over from a version of the guide that offered something else."""

    text: str


Value = OptionValue | TextValue


@dataclass(frozen=True)
class Range:
    """A closed interval, either end open.

    Written on the wire as ``low..high`` with either side allowed to be empty,
    and kept as the text the request carried: what it means depends on the
    item, and the item is not known until the answers are read.
    """

    low: str | None = None
    high: str | None = None


@dataclass(frozen=True)
class SurveyFilter:
    """Which survey answers an interview must have to stay in the corpus.

    Items are AND-ed and the values within an item are OR-ed, which is what a
    checklist per item looks like from the reader's side: "male or non-binary,
    and dissatisfied".
    """

    #: Chosen values per item. A checkbox answer holds several values at once
    #: and matches if any of them was chosen.
    values: dict[Coordinate, tuple[Value, ...]]
    #: Ranges per item, for the numeric and temporal ones.
    ranges: dict[Coordinate, Range]

    def __bool__(self) -> bool:
        return bool(self.values or self.ranges)

    @property
    def key(self) -> tuple:
        """A hashable form, so a request can resolve the filter once.

        The dataclass is frozen but holds dicts, which is the shape the parsing
        and the matching both want; this is what a cache can be keyed on.
        """
        return (
            frozenset(self.values.items()),
            frozenset(self.ranges.items()),
        )

    @property
    def coordinates(self) -> set[Coordinate]:
        return set(self.values) | set(self.ranges)


def answer_rows(
    session: Session,
    project_id: UUID,
    *,
    coordinates: Iterable[Coordinate] | None = None,
    include_synthetic: bool = False,
) -> list[AnswerRow]:
    """Every answer given to a survey item, one row per interview and item.

    Restricted to authored main questions -- a probe is a follow-up, not a
    variable -- so an interview answers each coordinate at most once and a
    cohort filter has a single value to test.

    Questions a condition skipped past are left out: they were never put to
    the respondent, so the message that follows one is the *next* question's,
    not an answer to it. Declined answers are left out too, for the plainer
    reason that a skip token is not a value anybody picked.
    """
    question = MessageTable
    answer = aliased(MessageTable)

    conditions = [
        question.project_id == project_id,
        question.role == MessageRole.ASSISTANT,
        has_survey_item(question.survey_item),
        question.section.is_not(None),
        question.main_question.is_not(None),
        func.coalesce(question.sub_question, 0) == 0,
        question.is_introduction.is_(False),
        question.outro.is_(False),
        question.can_answer.is_(True),
        question.skipped_by_condition.is_(False),
        answer.message_type != MessageType.CUSTOM_TOKEN,
        func.trim(answer.content).not_in(sorted(SKIP_TOKENS)),
        func.trim(answer.content) != "",
    ]

    pairs = list(coordinates) if coordinates is not None else None
    if pairs is not None:
        if not pairs:
            return []
        conditions.append(
            or_(
                *(
                    and_(
                        question.section == section,
                        question.main_question == main_question,
                    )
                    for section, main_question in pairs
                )
            )
        )

    if not include_synthetic:
        conditions.append(InterviewTable.type != InterviewType.SYNTHETIC_TEST)

    rows = session.execute(
        select(
            question.interview_id,
            question.section,
            question.main_question,
            question.survey_item,
            answer.content,
            InterviewTable.language,
        )
        .join(InterviewTable, InterviewTable.id == question.interview_id)
        .join(
            answer,
            and_(
                answer.interview_id == question.interview_id,
                answer.message_id == question.message_id + 1,
                answer.role == MessageRole.USER,
            ),
        )
        .where(*conditions)
    ).all()

    return [
        AnswerRow(
            interview_id=row.interview_id,
            coordinate=(row.section, row.main_question),
            content=row.content,
            item=row.survey_item,
            language=row.language or "",
        )
        for row in rows
    ]


def values_of(row: AnswerRow) -> list[Value]:
    """What one answer holds, as positions where the options have one.

    A checkbox answer holds several; everything else holds one. An answer that
    is not among the options this respondent saw is kept as its text -- a
    write-in is a real answer and worth being able to filter by, even though it
    cannot be matched across languages the way an option can.
    """
    options = options_of(row.item)
    if options is None:
        return []

    index = index_options(options)
    multiple = isinstance(row.item, CheckboxItem)
    parts = split_checkbox(row.content) if multiple else [row.content]

    values: list[Value] = []
    for part in parts:
        normalized = normalize_answer(part)
        if not normalized:
            continue
        position = index.get(normalized)
        values.append(
            OptionValue(position) if position is not None else TextValue(normalized)
        )
    return values


def _number(text: str) -> float | None:
    try:
        return float(text.strip().replace(",", "."))
    except ValueError:
        return None


def _moment(
    text: str, item: SurveyItem | None
) -> datetime.time | datetime.datetime | datetime.date | None:
    """An answer as the kind of instant its item asks for, or None if it is not one.

    An answer that no longer parses -- the item's type was changed after these
    interviews ran -- is not a date, and leaving it out of the comparison beats
    inventing an order for it.
    """
    text = text.strip()
    try:
        if isinstance(item, TimeItem):
            return datetime.time.fromisoformat(text)
        if isinstance(item, DatetimeItem):
            return datetime.datetime.fromisoformat(text)
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def _sorts_before(one: Any, other: Any) -> bool:
    """`one < other`, for two instants of a kind decided at runtime.

    A date, a datetime and a time are each ordered, but the item says which of
    the three this is and the type checker cannot follow that from here. The
    comparison is between two values parsed by the same call for the same item,
    so they are always the same kind.
    """
    return one < other


def in_range(row: AnswerRow, bounds: Range) -> bool:
    """Whether one answer falls inside a range, in the item's own terms.

    The bounds arrive as text because the request cannot know what they are
    until the item is read. They are parsed the same way the answer is, and a
    bound that will not parse is treated as no bound rather than as one nothing
    can satisfy -- the alternative is a filter that empties the view for a typo
    with nothing on screen to say so.
    """
    if isinstance(row.item, NUMERIC_TYPES):
        number = _number(row.content)
        if number is None:
            return False
        low = _number(bounds.low) if bounds.low else None
        high = _number(bounds.high) if bounds.high else None
        return (low is None or number >= low) and (high is None or number <= high)

    if isinstance(row.item, TEMPORAL_TYPES):
        moment = _moment(row.content, row.item)
        if moment is None:
            return False
        after = _moment(bounds.low, row.item) if bounds.low else None
        before = _moment(bounds.high, row.item) if bounds.high else None
        if after is not None and _sorts_before(moment, after):
            return False
        return before is None or not _sorts_before(before, moment)

    return False


def matching_interviews(
    session: Session,
    project_id: UUID,
    survey: SurveyFilter,
    *,
    include_synthetic: bool = False,
) -> set[UUID]:
    """The interviews whose answers satisfy every item of the filter.

    Resolved to a set of ids rather than compiled into the scan's SQL. The set
    is one row per interview per selected item -- a few hundred rows for a
    project of any size the explore view can draw -- and reading it in Python
    is what lets a checkbox's comma-joined answer, an option matched by
    position across languages and a date range be one filter rather than three
    dialects of SQL that have to agree on all of it.
    """
    rows = answer_rows(
        session,
        project_id,
        coordinates=survey.coordinates,
        include_synthetic=include_synthetic,
    )

    by_coordinate: dict[Coordinate, list[AnswerRow]] = {}
    for row in rows:
        by_coordinate.setdefault(row.coordinate, []).append(row)

    matching: set[UUID] | None = None
    for coordinate in sorted(survey.coordinates):
        wanted = set(survey.values.get(coordinate, ()))
        bounds = survey.ranges.get(coordinate)

        here = {
            row.interview_id
            for row in by_coordinate.get(coordinate, [])
            if (wanted and not wanted.isdisjoint(values_of(row)))
            or (bounds is not None and in_range(row, bounds))
        }
        matching = here if matching is None else matching & here

    return matching or set()
