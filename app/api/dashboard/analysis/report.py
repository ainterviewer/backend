"""Per-question answer distributions for the analysis > report page.

The unit here is the *authored question*: one entry per `(section,
main_question)` of the interview guide. Probes are left out — they are
generated during the interview, so there is no stable question for their
answers to be a distribution *of*.

Answers are not stored on the question. The interview writes the question as an
assistant message (carrying the `survey_item` snapshot it was asked with) and
the answer as the very next message of that interview, so the two are paired on
`message_id + 1` -- see `Interview.receive_data` in the `ainterviewer` library,
which inserts the answer at `current_message_id + 1`.
"""

import datetime
import statistics
from collections import Counter, defaultdict
from enum import StrEnum
from typing import Annotated, NamedTuple

from fastapi import APIRouter, Query
from pydantic import UUID4, BaseModel, Field
from sqlalchemy import and_, distinct, func, or_, select

from ainterviewer.interview_guides import SurveyItem
from ainterviewer.interview_guides.conditions import Conditions
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
from ainterviewer.types import (
    InterviewStatus,
    LanguageCode,
    MessageRole,
    MessageType,
)

from ....db.tables import (
    InterviewTable,
    MessageTable,
    ProjectLocalizationTable,
    TaskTable,
)
from ....db.types import InterviewType
from ....dependencies import DBSession, DemoToken, ProjectViewer
from .cohort import interview_cohort
from .histogram import HistogramBucket, compute_histogram_buckets

router = APIRouter(prefix="/report", tags=["report"])

# The task row the interview writes for every condition it evaluates. Spelled
# as the library spells it, typo and all -- it is the value in the column.
CONDITION_TASK = "evaulate_condition"

# What `response` holds when the conditions were met and the action fired.
CONDITION_FIRED = "True"

# A numeric item spanning at most this many whole numbers gets one bar per
# number rather than being binned: a 1-7 slider binned into 20 buckets reads as
# noise. The cap is on the *range*, not on how many distinct values were
# actually answered -- the bars have to stand for every number in between, or
# the axis stops being a number line.
MAX_EXACT_RANGE = 25

# How many distinct write-in answers a categorical item lists before the rest
# are folded into a single summary line.
MAX_OTHER_CATEGORIES = 8

# Longest span a date item draws one bar per day for. Past it the bars are
# months, which keeps a multi-year study from asking for a thousand of them.
MAX_TEMPORAL_DAYS = 92

# At or below this many answers a numeric/text item returns its raw values
# instead of a histogram: twelve answers binned into twenty buckets is not a
# distribution, it is a row of gaps.
MAX_SAMPLE_ANSWERS = 30

# What `SurveyItem.validate_answer` would call a skip. The interview stores
# these verbatim as the answer's content.
_SKIP_TOKENS = {token.value for token in CustomToken}


class DistributionKind(StrEnum):
    """Which of the distribution fields carries this item's data.

    The chart to draw follows from this, not from the survey item type: two
    item types that bin the same way (`slider` and `number`) are drawn the same
    way, and a question with no survey item still has a distribution -- the
    length of its free-text answers.
    """

    # `counts`, one bar per option/value.
    CATEGORICAL = "categorical"
    # `buckets` + `stats`, a histogram over the answered numbers.
    NUMERIC = "numeric"
    # `counts`, one bar per day (date/datetime) or per hour (time).
    TEMPORAL = "temporal"
    # `buckets` + `stats` over the word count of free-text answers.
    TEXT = "text"
    # Nothing: a statement the respondent could not answer (`can_answer` is
    # false). Carried so the guide's shape survives on the page -- every count
    # is zero and every collection empty.
    STATEMENT = "statement"


class CategoryCount(BaseModel):
    """One bar of a categorical distribution."""

    label: str
    count: int
    # An answer that is not one of the item's authored options: typed into an
    # "Other" field, or left over from an older version of the guide. Rendered
    # apart from the authored options, which are always shown even at zero.
    is_other: bool = False
    # How `count` splits across the languages the interviews ran in, keyed by
    # language code. What the bar is stacked by when the cohort spans more
    # than one language.
    by_language: dict[LanguageCode, int] = Field(default_factory=dict)


class DistributionBucket(HistogramBucket):
    """A histogram bucket carrying the same per-language split as a bar."""

    by_language: dict[LanguageCode, int] = Field(default_factory=dict)


class AnswerSample(BaseModel):
    """One answer's numeric value, kept verbatim rather than binned.

    A histogram needs enough answers to have a shape. Under
    `MAX_SAMPLE_ANSWERS` the individual values are returned instead, so the
    page can plot the answers themselves and not a row of one-tall bars.
    """

    value: float
    language: LanguageCode


class NumericStats(BaseModel):
    """Summary of the answered numbers behind a numeric/text distribution.

    Computed on the raw answers, so it keeps the precision the buckets round
    away.
    """

    min: float
    max: float
    mean: float
    median: float


class ItemDistribution(BaseModel):
    """The distribution of answers to one authored question."""

    section: int
    main_question: int
    # The question as authored in the project's default localization. Falls
    # back to the wording an interview actually asked when the question is no
    # longer in the guide.
    question: str
    kind: DistributionKind
    # The survey item the question was asked with, `None` for a free-text
    # question. Taken from the guide, so it carries the authored options even
    # when nobody picked some of them.
    item: SurveyItem | None
    # What had to hold for this question to be asked at all, straight from the
    # guide. Each condition names the question it reads by index, so the page
    # can both state the rule on this card and, reading the whole set, mark the
    # cards that gate others. `None` for an unconditional question.
    conditions: Conditions | None = None

    # Answer rate. `n_asked - n_answered - n_skipped` is the number of
    # respondents who saw the question and dropped out on it.
    n_asked: int
    n_answered: int
    n_skipped: int
    # Respondents who never saw the question because a condition on it did not
    # hold. Counted apart from `n_asked`, which is the denominator of the
    # answer rate: not being asked is not the same as being asked and not
    # answering, and folding the two together makes a gated question look like
    # one everybody abandons.
    n_not_asked_by_condition: int = 0

    # How often this question's own rule was evaluated, and how often it was
    # met and the action fired. Both zero for a question with no conditions --
    # and also where the firings cannot be attributed to a question with
    # certainty, which is the case for interviews that ran before the
    # evaluation recorded which question carried the rule. Absent rather than
    # approximate: this sits next to the rule on the card, and a number that
    # may belong to a neighbouring question is worse than none.
    n_condition_evaluated: int = 0
    n_condition_fired: int = 0

    counts: list[CategoryCount]
    buckets: list[DistributionBucket]
    stats: NumericStats | None
    # The individual values behind a numeric or text distribution, populated
    # only while there are few enough of them to draw one by one. When this is
    # non-empty it is what the page plots, and `buckets` is its fallback.
    samples: list[AnswerSample] = Field(default_factory=list)

    # The write-in tail `counts` leaves out: how many distinct values, and how
    # many answers they account for between them. Both zero unless the item is
    # categorical and had more write-ins than fit.
    n_other_hidden: int = 0
    n_other_hidden_count: int = 0


class GuideSection(BaseModel):
    """A section of the interview guide, for grouping the items under.

    Sourced from the project's default localization, so `description` may be in
    a different language than the dashboard page requesting it.
    """

    section: int
    description: str


class ItemDistributions(BaseModel):
    """Answer distributions for every question of a project's guide."""

    sections: list[GuideSection]
    items: list[ItemDistribution]
    # Every language the project has interviews in, regardless of the language
    # filter applied to this response, so the filter can offer all of them.
    languages: list[str]
    # Interviews in the filtered cohort. The denominator the answer rates are
    # read against.
    total_interviews: int


class _ValueCount(NamedTuple):
    """A row shaped the way `compute_histogram_buckets` expects."""

    value: float
    count: int


QuestionKey = tuple[int, int]


class _Answer(NamedTuple):
    """One answer, with what is needed to place it.

    `options` is the option list the respondent actually saw -- the snapshot
    stored on the question message, in the language that interview ran in.
    `None` for a question with no survey item.
    """

    content: str
    language: LanguageCode
    options: list[str] | None


class _Firings(NamedTuple):
    """How often one question's rule was evaluated, and how often it fired."""

    evaluated: int
    fired: int


class _Collected:
    """Raw answers to one authored question, before they become a distribution."""

    def __init__(self) -> None:
        self.n_asked = 0
        self.n_skipped = 0
        self.n_not_asked_by_condition = 0
        self.answers: list[_Answer] = []
        # The wording and item snapshot an interview actually used, as a
        # fallback for questions the guide no longer has.
        self.observed_question: str | None = None
        self.observed_item: SurveyItem | None = None


class _OtherEntry:
    """A write-in value being accumulated across answers."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.count = 0
        self.by_language: Counter[str] = Counter()


def _numeric_distribution(
    samples: list[AnswerSample],
) -> tuple[list[DistributionBucket], NumericStats]:
    """Bin numbers into bars, one per value while there are few enough of them."""
    values = [sample.value for sample in samples]
    counter = Counter(values)
    stats = NumericStats(
        min=min(values),
        max=max(values),
        mean=statistics.fmean(values),
        median=statistics.median(values),
    )

    lowest, highest = int(min(values)), int(max(values))

    if all(value.is_integer() for value in counter) and (
        highest - lowest + 1 <= MAX_EXACT_RANGE
    ):
        exact: dict[int, Counter[str]] = defaultdict(Counter)
        for sample in samples:
            exact[int(sample.value)][sample.language] += 1

        # Every number in the range gets a bar, answered or not. Listing only
        # the values someone gave puts them on a band scale at even spacing,
        # so 10 sits as far from 30 as 32 does from 33 and the axis reads as a
        # list of labels rather than as a measurement.
        buckets = [
            DistributionBucket(
                value=value,
                count=sum(exact[value].values()),
                label=str(value),
                by_language=dict(exact[value]),
            )
            for value in range(lowest, highest + 1)
        ]
        return buckets, stats

    rows = [_ValueCount(value, count) for value, count in sorted(counter.items())]
    binned = compute_histogram_buckets(rows)

    # The helper picks the edges; the samples are then re-walked against them
    # so each bucket knows how it splits by language. Every bucket it returns
    # is the same width, so the edge a value falls behind is arithmetic.
    splits: list[Counter[str]] = [Counter() for _ in binned]
    if len(binned) == 1:
        for sample in samples:
            splits[0][sample.language] += 1
    else:
        start = binned[0].value
        step = binned[1].value - binned[0].value
        for sample in samples:
            index = int((sample.value - start) // step)
            index = max(0, min(index, len(binned) - 1))
            splits[index][sample.language] += 1

    buckets = [
        DistributionBucket(
            value=bucket.value,
            count=bucket.count,
            label=bucket.label,
            by_language=dict(split),
        )
        for bucket, split in zip(binned, splits)
    ]
    return buckets, stats


def _normalize(value: str) -> str:
    """The form two answers are compared on.

    An answer arrives as the text the respondent's client submitted, which for
    a conversational interview is not always a verbatim copy of the option:
    `"yes"` and `"Yes"` are the same choice and have to land on the same bar,
    or an item ends up with an authored option beside an identical-looking
    write-in.
    """
    return " ".join(value.split()).casefold()


def _index_options(options: list[str]) -> dict[str, int]:
    """Map each option's normalized form to its position. First spelling wins."""
    index: dict[str, int] = {}
    for position, option in enumerate(options):
        index.setdefault(_normalize(option), position)
    return index


def _split_checkbox(answer: str) -> list[str]:
    """Undo the `", "` join the checkbox widget submits its selection with."""
    return [part.strip() for part in answer.split(",") if part.strip()]


def _categorical_counts(
    options: list[str], answers: list[_Answer], *, multi: bool
) -> tuple[list[CategoryCount], int, int]:
    """Count answers against the authored options, keeping the authored order.

    Every option gets a bar even at zero -- "nobody picked this" is a result.

    An answer is placed by *position* in the option list the respondent saw,
    not by its text. The same item asked in two languages offers the same
    choices translated, so "Female" and "Kvinde" are one bar, not two -- and
    without this every non-default-language answer would land in the write-in
    tail. Position is only trusted while the snapshot has as many options as
    the authored item: a different length means the item was edited between
    the two and the positions no longer line up, so those answers fall back to
    matching on text.

    Write-ins have no natural bound: a conversational interview can put a
    distinct wording on nearly every respondent, which buries the authored
    options under a hundred one-answer bars. Only the most common ones are
    returned; the tail is reported as a pair of counts for the caller to
    summarise.

    Returns `(counts, hidden_values, hidden_answers)`.
    """
    authored = _index_options(options)
    counts = [0] * len(options)
    by_language: list[Counter[str]] = [Counter() for _ in options]
    other: dict[str, _OtherEntry] = {}

    for answer in answers:
        snapshot = (
            _index_options(answer.options)
            if answer.options and len(answer.options) == len(options)
            else None
        )

        for value in _split_checkbox(answer.content) if multi else [answer.content]:
            normalized = _normalize(value)
            if not normalized:
                continue

            position = None
            if snapshot is not None:
                position = snapshot.get(normalized)
            if position is None:
                position = authored.get(normalized)

            if position is not None:
                counts[position] += 1
                by_language[position][answer.language] += 1
                continue

            entry = other.setdefault(normalized, _OtherEntry(value.strip()))
            entry.count += 1
            entry.by_language[answer.language] += 1

    ranked = sorted(other.values(), key=lambda entry: (-entry.count, entry.label))
    shown, hidden = ranked[:MAX_OTHER_CATEGORIES], ranked[MAX_OTHER_CATEGORIES:]

    bars = [
        CategoryCount(
            label=option,
            count=counts[position],
            by_language=dict(by_language[position]),
        )
        for position, option in enumerate(options)
    ] + [
        CategoryCount(
            label=entry.label,
            count=entry.count,
            is_other=True,
            by_language=dict(entry.by_language),
        )
        for entry in shown
    ]

    return bars, len(hidden), sum(entry.count for entry in hidden)


def _temporal_counts(answers: list[_Answer], item: SurveyItem) -> list[CategoryCount]:
    """Bin date/datetime answers by day and time answers by hour.

    Zero-filled across the whole range for the same reason the numeric bars
    are: a bar per *answered* day puts a week's gap and a day's gap the same
    distance apart. A range too long to draw a bar per day falls back to a bar
    per month rather than to uneven spacing.
    """
    buckets: dict[str, Counter[str]] = defaultdict(Counter)

    if isinstance(item, TimeItem):
        for answer in answers:
            try:
                hour = datetime.time.fromisoformat(answer.content).hour
            except ValueError:
                continue
            buckets[f"{hour:02d}:00"][answer.language] += 1

        if not buckets:
            return []

        # A clock is a fixed range whatever was answered, so all of it is
        # drawn: "nobody answered at night" is part of the distribution.
        labels = [f"{hour:02d}:00" for hour in range(24)]
        return _fill(labels, buckets)

    days: list[tuple[datetime.date, str]] = []
    for answer in answers:
        try:
            if isinstance(item, DatetimeItem):
                day = datetime.datetime.fromisoformat(answer.content).date()
            else:
                day = datetime.date.fromisoformat(answer.content)
        except ValueError:
            # An answer that no longer parses (the item's type was changed
            # after these interviews ran) is not a date; leaving it out beats
            # inventing a bucket for it.
            continue
        days.append((day, answer.language))

    if not days:
        return []

    first, last = min(day for day, _ in days), max(day for day, _ in days)

    if (last - first).days + 1 <= MAX_TEMPORAL_DAYS:
        for day, language in days:
            buckets[day.isoformat()][language] += 1
        labels = [
            (first + datetime.timedelta(days=offset)).isoformat()
            for offset in range((last - first).days + 1)
        ]
    else:
        for day, language in days:
            buckets[f"{day.year:04d}-{day.month:02d}"][language] += 1
        labels = _month_range(first, last)

    return _fill(labels, buckets)


def _fill(labels: list[str], buckets: dict[str, Counter[str]]) -> list[CategoryCount]:
    """One bar per label, including the labels nothing landed on."""
    return [
        CategoryCount(
            label=label,
            count=sum(buckets[label].values()),
            by_language=dict(buckets[label]),
        )
        for label in labels
    ]


def _month_range(first: datetime.date, last: datetime.date) -> list[str]:
    """Every `YYYY-MM` from `first` to `last` inclusive."""
    labels = []
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        labels.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return labels


def _build_distribution(
    section: int,
    main_question: int,
    question: str,
    item: SurveyItem | None,
    conditions: Conditions | None,
    collected: _Collected,
    firings: _Firings,
) -> ItemDistribution:
    answers = collected.answers
    kind = DistributionKind.TEXT
    counts: list[CategoryCount] = []
    buckets: list[DistributionBucket] = []
    samples: list[AnswerSample] = []
    stats: NumericStats | None = None
    hidden_values = 0
    hidden_answers = 0

    def numeric(values: list[AnswerSample]) -> None:
        nonlocal buckets, samples, stats
        if not values:
            return
        buckets, stats = _numeric_distribution(values)
        # Both are returned when there are few answers: the page draws the
        # samples and keeps the buckets as the fallback for a chart that would
        # rather bin them.
        if len(values) <= MAX_SAMPLE_ANSWERS:
            samples = values

    match item:
        case RadioItem() | LikertItem():
            kind = DistributionKind.CATEGORICAL
            counts, hidden_values, hidden_answers = _categorical_counts(
                item.options, answers, multi=False
            )

        case CheckboxItem():
            kind = DistributionKind.CATEGORICAL
            counts, hidden_values, hidden_answers = _categorical_counts(
                item.options, answers, multi=True
            )

        case SliderItem() | NumberItem():
            kind = DistributionKind.NUMERIC
            parsed: list[AnswerSample] = []
            for answer in answers:
                try:
                    parsed.append(
                        AnswerSample(
                            value=float(answer.content), language=answer.language
                        )
                    )
                except ValueError:
                    continue
            numeric(parsed)

        case DateItem() | DatetimeItem() | TimeItem():
            kind = DistributionKind.TEMPORAL
            counts = _temporal_counts(answers, item)

        case _:
            # No survey item: the answer is free text, and what is worth
            # plotting about it is how much of it there was.
            kind = DistributionKind.TEXT
            numeric(
                [
                    AnswerSample(
                        value=float(len(answer.content.split())),
                        language=answer.language,
                    )
                    for answer in answers
                ]
            )

    return ItemDistribution(
        section=section,
        main_question=main_question,
        question=question,
        kind=kind,
        item=item,
        conditions=conditions,
        n_asked=collected.n_asked,
        n_answered=len(answers),
        n_skipped=collected.n_skipped,
        n_not_asked_by_condition=collected.n_not_asked_by_condition,
        n_condition_evaluated=firings.evaluated,
        n_condition_fired=firings.fired,
        counts=counts,
        buckets=buckets,
        stats=stats,
        samples=samples,
        n_other_hidden=hidden_values,
        n_other_hidden_count=hidden_answers,
    )


def _checked_after_answer(conditions: Conditions | None, key: QuestionKey) -> bool:
    """Whether a rule reads the answer to the question carrying it.

    Such a rule cannot be evaluated until that question has been answered, so
    the interview checks it *after* asking rather than before -- see
    `Interview.should_check_condition_after_question`. It therefore decides not
    whether the question was put, but how much further the interview goes from
    it, and it is the one kind of rule whose firings the card has no other way
    to show.
    """
    if conditions is None:
        return False
    return any(
        (condition.question_context.section, condition.question_context.question) == key
        for condition in conditions.conditions
    )


def _carrier_key(context: str | None) -> QuestionKey | None:
    """The question a recorded evaluation hangs off, or `None` if unrecorded."""
    if not context:
        return None
    section, _, question = context.partition(":")
    try:
        return (int(section), int(question))
    except ValueError:
        return None


def _unattributable(
    order: list[QuestionKey], authored_conditions: dict[QuestionKey, Conditions | None]
) -> set[QuestionKey]:
    """Questions whose un-carried evaluations cannot be told from the next one's.

    A rule checked *before* its question is asked is written against whatever
    message came last, which is the previous question's. So an evaluation
    landing on question K's messages belongs either to K's own after-the-answer
    rule or to the pre-check of the question that follows K -- and when that
    successor's rule happens to read K, the two are identical in every column.
    Rather than guess, K is left without a count.

    Only evaluations that predate the carrier being recorded need this; see
    `_carrier_key`.
    """
    ambiguous: set[QuestionKey] = set()

    for index, key in enumerate(order[:-1]):
        following = order[index + 1]
        rule = authored_conditions.get(following)
        if rule is None or not rule.conditions:
            continue
        # A successor whose own rule is checked after its answer writes against
        # its own messages, not against K's.
        if _checked_after_answer(rule, following):
            continue
        ambiguous.add(key)

    return ambiguous


def _condition_firings(
    session,
    project_id: UUID4,
    interviews,
    order: list[QuestionKey],
    authored_conditions: dict[QuestionKey, Conditions | None],
) -> dict[QuestionKey, _Firings]:
    """How often each question's rule was evaluated and met, across the cohort.

    Read off the task the interview writes for every evaluation, which records
    the verdict but -- before the carrier was added -- not which question the
    rule belonged to. Those older rows are attributed through the message the
    evaluation was written against, and only where that attribution is not
    ambiguous.
    """
    rows = session.execute(
        select(
            TaskTable.context,
            TaskTable.response,
            MessageTable.section,
            MessageTable.main_question,
        )
        .join(interviews, interviews.c.id == TaskTable.interview_id)
        # Outer: an evaluation written against a timed message or the
        # introduction has no question indices, and one carrying its own
        # carrier does not need them.
        .outerjoin(
            MessageTable,
            and_(
                MessageTable.interview_id == TaskTable.interview_id,
                MessageTable.message_id == TaskTable.message_id,
            ),
        )
        .where(
            TaskTable.project_id == project_id,
            TaskTable.task == CONDITION_TASK,
        )
    ).all()

    ambiguous = _unattributable(order, authored_conditions)
    tally: dict[QuestionKey, list[int]] = defaultdict(lambda: [0, 0])

    for row in rows:
        key = _carrier_key(row.context)

        if key is None:
            if row.section is None or row.main_question is None:
                continue
            key = (row.section, row.main_question)
            # Without a carrier, only a rule the interview checks after its own
            # answer is known to have been written against its own question.
            if key in ambiguous or not _checked_after_answer(
                authored_conditions.get(key), key
            ):
                continue

        counts = tally[key]
        counts[0] += 1
        if row.response == CONDITION_FIRED:
            counts[1] += 1

    return {
        key: _Firings(evaluated=n, fired=fired) for key, (n, fired) in tally.items()
    }


@router.get(
    "/projects/{project_id}/item-distributions",
    description="Get the distribution of answers to each question of a project's interview guide",
)
def get_project_item_distributions(
    project_id: UUID4,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectViewer,
    interview_types: Annotated[
        list[InterviewType],
        Query(default_factory=lambda: [InterviewType.DISTRIBUTED]),
    ],
    languages: Annotated[
        list[str] | None,
        Query(description="Restrict to interviews conducted in these languages"),
    ] = None,
    completed_only: Annotated[
        bool, Query(description="Count only interviews that reached the end")
    ] = False,
    deduplicate_by_pid: Annotated[
        bool,
        Query(
            description=(
                "Count one interview per participant ID, keeping the one that "
                "got furthest"
            )
        ),
    ] = False,
) -> ItemDistributions:
    # NOTE: a plain `def` on purpose -- see the note in `monitoring.py`. The
    # session is synchronous, so an `async def` would block the event loop for
    # the duration of every query.
    session = db.session

    interview_conditions = [
        InterviewTable.project_id == project_id,
        InterviewTable.type.in_(interview_types),
    ]

    # Offered by the filter UI, so it must not itself be narrowed by the
    # language filter -- otherwise picking one language hides all the others.
    available_languages = [
        language
        for language in session.execute(
            select(distinct(InterviewTable.language))
            .where(*interview_conditions)
            .order_by(InterviewTable.language)
        ).scalars()
        if language
    ]

    if languages:
        interview_conditions.append(InterviewTable.language.in_(languages))
    if completed_only:
        interview_conditions.append(InterviewTable.status == InterviewStatus.COMPLETED)

    # Deduplication is applied here and nowhere else: every count below is
    # derived from this CTE, so dropping a repeat visit drops it from the
    # asked/answered totals, the per-option tallies and the condition firings
    # in one move. Shared with the monitoring page so the two agree about who
    # the cohort is.
    interviews = interview_cohort(
        (
            InterviewTable.id.label("id"),
            InterviewTable.language.label("language"),
        ),
        interview_conditions,
        deduplicate_by_pid=deduplicate_by_pid,
    )

    total_interviews = (
        session.execute(select(func.count()).select_from(interviews)).scalar_one() or 0
    )

    # One pass over the cohort's question-level messages. Questions and answers
    # are fetched together and paired in Python rather than by a self-join:
    # both sides are needed in full anyway (the answers to count, the questions
    # to count how often each was asked), and the JSONB `survey_item` only ever
    # decodes once this way.
    rows = session.execute(
        select(
            MessageTable.interview_id,
            MessageTable.message_id,
            MessageTable.role,
            MessageTable.content,
            MessageTable.message_type,
            MessageTable.section,
            MessageTable.main_question,
            MessageTable.survey_item,
            MessageTable.skipped_by_condition,
            interviews.c.language,
        )
        .join(interviews, interviews.c.id == MessageTable.interview_id)
        .where(
            MessageTable.project_id == project_id,
            or_(
                # The question: an authored main question. Probes
                # (`sub_question > 0`), the introduction, the outro and
                # statements the respondent cannot answer are excluded. A
                # question a condition skipped past *is* fetched -- it was
                # never put to the respondent, so it has no answer, but how
                # often a gate closed is exactly what the condition on the card
                # is read against.
                (
                    (MessageTable.role == MessageRole.ASSISTANT)
                    & (MessageTable.section.is_not(None))
                    & (MessageTable.main_question.is_not(None))
                    & (func.coalesce(MessageTable.sub_question, 0) == 0)
                    & (MessageTable.is_introduction.is_(False))
                    & (MessageTable.outro.is_(False))
                    & (MessageTable.can_answer.is_(True))
                ),
                # Every answer, unfiltered: which ones are answers to a main
                # question is decided by the `message_id + 1` pairing below.
                MessageTable.role == MessageRole.USER,
            ),
        )
    ).all()

    # Indexed first, because a question is paired with the answer that follows
    # it and the rows arrive in no guaranteed order.
    answers_by_position: dict[tuple[UUID4, int], tuple[str, MessageType]] = {
        (row.interview_id, row.message_id): (row.content, row.message_type)
        for row in rows
        if row.role == MessageRole.USER
    }

    collected: dict[QuestionKey, _Collected] = defaultdict(_Collected)

    for row in rows:
        if row.role != MessageRole.ASSISTANT:
            continue

        key: QuestionKey = (row.section, row.main_question)
        bucket = collected[key]

        if row.skipped_by_condition:
            # Written to the transcript so the guide's shape survives there,
            # but never shown to the respondent: it is not an asking, and the
            # message that follows it is the *next* question's, not an answer
            # to this one.
            bucket.n_not_asked_by_condition += 1
            continue

        bucket.n_asked += 1
        if bucket.observed_question is None:
            bucket.observed_question = row.content
        if bucket.observed_item is None:
            bucket.observed_item = row.survey_item

        answer = answers_by_position.get((row.interview_id, row.message_id + 1))
        if answer is None:
            # Asked but never answered: the respondent dropped out here.
            continue

        content, message_type = answer
        if message_type == MessageType.CUSTOM_TOKEN or content in _SKIP_TOKENS:
            bucket.n_skipped += 1
        else:
            # The snapshot is taken off the *question* message, so the options
            # travelling with the answer are the ones this respondent actually
            # saw, in the language their interview ran in.
            bucket.answers.append(
                _Answer(
                    content=content,
                    language=row.language or "",
                    options=getattr(row.survey_item, "options", None),
                )
            )

    # Zero-fill from the project's default localization, so a question nobody
    # reached still gets a (empty) chart and the sections can be labelled.
    #
    # Note the indices on a message are positions in the order the respondent
    # was actually asked, which for a shuffled section is not the guide's
    # authored order. Pairing them with the guide is therefore an
    # approximation of "the Nth question of section M".
    default_guide = session.execute(
        select(ProjectLocalizationTable.interview_guide).where(
            ProjectLocalizationTable.project_id == project_id,
            ProjectLocalizationTable.is_default.is_(True),
        )
    ).scalar_one_or_none()

    sections: list[GuideSection] = []
    authored: dict[
        QuestionKey, tuple[str, SurveyItem | None, bool, Conditions | None]
    ] = {}

    if default_guide is not None:
        for section_idx, section in enumerate(default_guide.question_sections):
            sections.append(
                GuideSection(section=section_idx, description=section.description)
            )
            for question_idx, question in enumerate(section.questions):
                # A statement (`can_answer` false) is kept rather than skipped.
                # It has no answers to distribute -- the query above filters it
                # out -- but it is part of what the respondent was read, and a
                # guide that shows only its answerable half misrepresents the
                # interview.
                authored[(section_idx, question_idx)] = (
                    question.main_question,
                    question.survey_item,
                    question.can_answer,
                    question.conditions,
                )

    # Keyed apart from `authored` so the firing counts can be worked out before
    # the items are built, and in the guide's own order -- which is what tells
    # an un-carried evaluation on one question from the next question's
    # pre-check.
    authored_conditions: dict[QuestionKey, Conditions | None] = {
        key: entry[3] for key, entry in authored.items()
    }
    firings_by_key = _condition_firings(
        session, project_id, interviews, list(authored), authored_conditions
    )

    # Union, not replacement: the guide above is the current editable draft,
    # while these interviews ran against per-interview snapshots taken when
    # they were created. Answers to a question the draft no longer has are
    # still answers, and are carried on their observed wording and item.
    ordered_keys = list(authored)
    for key in sorted(collected):
        if key not in authored:
            ordered_keys.append(key)

    items: list[ItemDistribution] = []
    for section_idx, question_idx in ordered_keys:
        bucket = collected.get((section_idx, question_idx), _Collected())
        firings = firings_by_key.get((section_idx, question_idx), _Firings(0, 0))
        question, item, answerable, conditions = authored.get(
            (section_idx, question_idx),
            # A question the draft no longer has: its conditions went with it,
            # so the card states no rule rather than an outdated one.
            (bucket.observed_question or "", bucket.observed_item, True, None),
        )
        if not answerable:
            items.append(
                ItemDistribution(
                    section=section_idx,
                    main_question=question_idx,
                    question=question,
                    kind=DistributionKind.STATEMENT,
                    item=None,
                    conditions=conditions,
                    n_asked=0,
                    n_answered=0,
                    n_skipped=0,
                    n_not_asked_by_condition=bucket.n_not_asked_by_condition,
                    n_condition_evaluated=firings.evaluated,
                    n_condition_fired=firings.fired,
                    counts=[],
                    buckets=[],
                    stats=None,
                )
            )
            continue
        items.append(
            _build_distribution(
                section_idx, question_idx, question, item, conditions, bucket, firings
            )
        )

    return ItemDistributions(
        sections=sections,
        items=items,
        languages=available_languages,
        total_interviews=total_interviews,
    )
