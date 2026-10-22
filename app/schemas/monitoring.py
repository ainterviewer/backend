"""The monitoring dashboard and the per-question answer report."""

from __future__ import annotations

import datetime
from enum import StrEnum

from pydantic import Field

from ainterviewer.interview_guides import SurveyItem
from ainterviewer.interview_guides.conditions import Conditions
from ainterviewer.types import InterviewStatus, LanguageCode

from ..types import InterviewType
from .common import Schema


class CohortParams(Schema):
    """Which of a project's interviews an analysis view counts."""

    interview_types: list[InterviewType] = Field(
        default_factory=lambda: [InterviewType.DISTRIBUTED]
    )
    deduplicate_by_pid: bool = Field(
        default=False,
        description=(
            "Count one interview per participant ID, keeping the one that got furthest"
        ),
    )


class MonitoringParams(CohortParams):
    start_date: datetime.datetime | None = None
    end_date: datetime.datetime | None = None


class ItemDistributionParams(CohortParams):
    languages: list[str] | None = Field(
        default=None,
        description="Restrict to interviews conducted in these languages",
    )
    completed_only: bool = Field(
        default=False, description="Count only interviews that reached the end"
    )


class HistogramBucket(Schema):
    """A value-count pair for histogram use."""

    value: int
    count: int
    label: str


class InterviewStatusCount(Schema):
    """Count of interviews by status."""

    status: InterviewStatus
    count: int


class LanguageCount(Schema):
    """Count of interviews conducted in a given language."""

    language: LanguageCode
    count: int


class DailyInterviewCount(Schema):
    """Count of interviews created per day."""

    date: datetime.date
    count: int
    completed_count: int


class InterviewTimeOfDayCount(Schema):
    time: str
    count: int


class InterviewDurationStats(Schema):
    """Statistics about interview duration (active time spent).

    Over the same gap-capped durations the duration histogram is binned from,
    so the summary and the chart describe the same quantity.
    """

    min_seconds: int
    max_seconds: int
    avg_seconds: float
    sum_seconds: float


class MessageCountStats(Schema):
    """Statistics about message counts per interview."""

    min_messages: int
    max_messages: int
    avg_messages: float
    sum_messages: int


class DropoutStage(StrEnum):
    """Where in the interview an inactive respondent stopped.

    Dropouts do not all land on a question: an interview can be abandoned
    before a single message exists, during the introduction, or on the outro.
    Those three stages have no `(section, main_question)` to group by, so they
    are carried as explicit stages instead of being dropped.
    """

    NEVER_STARTED = "never_started"
    INTRODUCTION = "introduction"
    QUESTION = "question"
    OUTRO = "outro"


class DropoutPoint(Schema):
    """Count of dropouts at a specific point in the interview.

    `section`, `main_question` and `sub_question` are the zero-based indices
    recorded on the last message, and are only populated for
    `DropoutStage.QUESTION`.
    """

    stage: DropoutStage
    section: int | None
    main_question: int | None
    sub_question: int | None
    count: int


class DropoutSection(Schema):
    """A section of the interview guide, for grouping dropout points.

    Sourced from the project's default localization, so `description` and
    `questions` may be in a different language than the dashboard page
    requesting these stats.
    """

    section: int
    description: str
    # The section's main questions, in the guide's authored order, so a dropout
    # point can be labelled with the text of the question it stopped on.
    # Positional: `questions[main_question]`. Probes have no entry here — they
    # are generated during the interview, not authored in the guide.
    questions: list[str]


class MonitoringStats(Schema):
    """Aggregated monitoring statistics for a project."""

    # Basic counts
    total_interviews: int
    completion_rate: float

    # Breakdowns
    interviews_by_status: list[InterviewStatusCount]
    interviews_by_language: list[LanguageCount]

    # Time series data
    interviews_over_time: list[DailyInterviewCount]
    interviews_by_time_of_day: list[InterviewTimeOfDayCount]

    # Duration/engagement stats
    duration_stats: InterviewDurationStats | None
    message_count_stats: MessageCountStats | None

    # Histogram distributions
    duration_histogram: list[HistogramBucket]
    # How many pauses between consecutive messages ran past the cap and were
    # counted as the cap, and what that cap is. Reported so the chart can say
    # that the durations it shows are active time rather than elapsed time.
    duration_gaps_capped: int
    duration_gap_cap_seconds: int
    message_count_histogram: list[HistogramBucket]
    message_length_histogram: list[HistogramBucket]

    # Dropout analysis
    dropout_stats: list[DropoutPoint]
    dropout_sections: list[DropoutSection]

    # Participation. `total_participants` is the project's full participant
    # roster, which carries no interview dates, so a `start_date`/`end_date`
    # filter narrows `participants_completed` only. A project with no linked
    # participants (anonymous-link distribution) reports 0/0 and a null rate;
    # the dashboard hides the card in that case rather than showing 0%.
    total_participants: int
    participants_completed: int
    participation_rate: float | None

    # Denominator for `dropout_stats`: every dropout point is one inactive
    # interview, so these two must agree.
    total_inactive: int


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


class CategoryCount(Schema):
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


class AnswerSample(Schema):
    """One answer's numeric value, kept verbatim rather than binned.

    A histogram needs enough answers to have a shape. Under
    `MAX_SAMPLE_ANSWERS` the individual values are returned instead, so the
    page can plot the answers themselves and not a row of one-tall bars.
    """

    value: float
    language: LanguageCode


class NumericStats(Schema):
    """Summary of the answered numbers behind a numeric/text distribution.

    Computed on the raw answers, so it keeps the precision the buckets round
    away.
    """

    min: float
    max: float
    mean: float
    median: float


class ItemDistribution(Schema):
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


class GuideSection(Schema):
    """A section of the interview guide, for grouping the items under.

    Sourced from the project's default localization, so `description` may be in
    a different language than the dashboard page requesting it.
    """

    section: int
    description: str


class ItemDistributions(Schema):
    """Answer distributions for every question of a project's guide."""

    sections: list[GuideSection]
    items: list[ItemDistribution]
    # Every language the project has interviews in, regardless of the language
    # filter applied to this response, so the filter can offer all of them.
    languages: list[str]
    # Interviews in the filtered cohort. The denominator the answer rates are
    # read against.
    total_interviews: int
