"""Histogram binning shared by the analysis endpoints.

Lives in its own module because both `monitoring` (interview-level histograms)
and `report` (per-item histograms) bin value/count rows the same way,
and an axis that reads 300, 350, 400 in one place should read the same in the
other.
"""

import bisect
import math
from collections.abc import Sequence
from typing import NamedTuple

from pydantic import BaseModel


class ValueCount(NamedTuple):
    """A value/count row, for callers that derive rows rather than query them.

    The binning functions take any sequence of objects with `value` and `count`,
    which is normally a SQLAlchemy `Row`; this is that shape by hand.
    """

    value: float
    count: int


class HistogramBucket(BaseModel):
    """A value-count pair for histogram use."""

    value: int
    count: int
    label: str


# Mantissas that read as "round" on an axis. Only whole-number steps are used,
# so 2.5 is only ever picked from a magnitude of 10 upwards (25, 250, ...).
_NICE_MANTISSAS = (1, 2, 2.5, 5, 10)


def nice_step(raw_step: float) -> int:
    """Round `raw_step` up to the next whole, human-readable step.

    Produces 1, 2, 5, 10, 25, 50, 100, 250, ... rather than the arbitrary
    integers a plain `ceil(span / num_bins)` yields (43, 39, ...), so axis
    labels land on values a reader can scan.
    """
    if raw_step <= 1:
        return 1

    magnitude = 10 ** math.floor(math.log10(raw_step))
    for mantissa in _NICE_MANTISSAS:
        candidate = mantissa * magnitude
        if candidate >= raw_step and float(candidate).is_integer():
            return int(candidate)

    # log10 rounding can leave us just past 10 * magnitude; the next decade is
    # always nice and always large enough.
    return int(10 * magnitude)


def compute_histogram_buckets(
    data_rows: Sequence, num_bins: int = 20
) -> list[HistogramBucket]:
    """Bin grouped value/count rows into `num_bins` evenly spaced buckets.

    Bucket width is a "nice" number and the first bucket edge is snapped down to
    a multiple of that width, so the axis reads 300, 350, 400, ... instead of
    321, 364, 407, ...
    """
    if not data_rows:
        return []

    # data_rows are expected to be sorted by value
    # and have .value and .count attributes
    min_val = data_rows[0].value
    max_val = data_rows[-1].value

    span = max_val - min_val

    # If single value or no span, return single bucket
    if span == 0:
        total_count = sum(row.count for row in data_rows)
        return [
            HistogramBucket(
                value=int(min_val), count=total_count, label=str(int(min_val))
            )
        ]

    # Snapping the origin down costs at most one bucket of headroom, so size the
    # step against `num_bins - 1` buckets and then verify coverage explicitly.
    step = nice_step((span + 1) / (num_bins - 1))
    start = int(math.floor(min_val / step) * step)
    while start + num_bins * step <= max_val:
        step = nice_step(step + 1)
        start = int(math.floor(min_val / step) * step)

    # Only as many buckets as it takes to reach the largest value. `num_bins` is
    # a ceiling, not a quota: a nice step often covers the data in fewer, and
    # emitting the full count anyway padded the axis with empty buckets past the
    # last observation -- a working-hours histogram topping out at 60 ran on to
    # 110 with nine dead bars.
    bucket_count = min(num_bins, int((max_val - start) // step) + 1)

    buckets = [0] * bucket_count
    for row in data_rows:
        idx = int((row.value - start) // step)
        # Defensive clamp; the coverage loop above should make this unreachable.
        idx = max(0, min(idx, bucket_count - 1))
        buckets[idx] += row.count

    return [
        HistogramBucket(
            value=start + i * step,
            count=count,
            label=f"{start + i * step}-{start + (i + 1) * step}",
        )
        for i, count in enumerate(buckets)
    ]


# Edge mantissas for a log axis, in decreasing resolution: 1-2-5 per decade
# first, decades only when that overflows `num_bins`.
_LOG_MANTISSA_SETS = ((1, 2, 5), (1,))


def _nice_log_edges(low: int, high: int, num_bins: int) -> list[int]:
    """Bucket edges from `low` to past `high`, spaced 1, 2, 5, 10, 20, ...

    Returns one more edge than there are buckets: the last entry is the top of
    the final bucket, not a bucket of its own.
    """
    exp_lo = math.floor(math.log10(low))
    exp_hi = math.floor(math.log10(high)) + 1

    for mantissas in _LOG_MANTISSA_SETS:
        candidates = [
            int(mantissa * 10**exponent)
            for exponent in range(exp_lo, exp_hi + 1)
            for mantissa in mantissas
        ]
        # Keep the single edge at or below `low` -- it is the first bucket's
        # floor -- and everything up to the first edge past `high`, which closes
        # the last bucket.
        start = max(i for i, edge in enumerate(candidates) if edge <= low)
        end = min(i for i, edge in enumerate(candidates) if edge > high)
        edges = candidates[start : end + 1]
        if len(edges) - 1 <= num_bins:
            return edges

    return edges


def compute_log_histogram_buckets(
    data_rows: Sequence, num_bins: int = 20
) -> list[HistogramBucket]:
    """Bin grouped value/count rows into logarithmically spaced buckets.

    For a distribution spanning orders of magnitude -- message lengths, where
    most answers are a line and a few are essays -- even bins put nearly every
    observation in the first one or two bars. Log-spaced edges spread the mass
    out instead.

    The chart's x axis is a band scale, so the bars come out evenly spaced
    whatever the edges are; it is the labels that turn it into a log axis.
    """
    if not data_rows:
        return []

    # data_rows are expected to be sorted by value
    min_val = int(data_rows[0].value)
    max_val = int(data_rows[-1].value)

    if min_val == max_val:
        total_count = sum(row.count for row in data_rows)
        return [HistogramBucket(value=min_val, count=total_count, label=str(min_val))]

    # log10 needs a positive floor. A zero-length message is its own bucket
    # rather than being folded into the first real one.
    edges = _nice_log_edges(max(min_val, 1), max_val, num_bins)
    if min_val < edges[0]:
        edges.insert(0, min_val)

    buckets = [0] * (len(edges) - 1)
    for row in data_rows:
        # Rightmost edge that is <= the value; `edges[0] <= min_val` makes the
        # -1 safe, and the clamp catches a value sitting on the closing edge.
        idx = bisect.bisect_right(edges, int(row.value)) - 1
        buckets[min(idx, len(buckets) - 1)] += row.count

    return [
        HistogramBucket(
            value=edges[i],
            count=count,
            label=f"{edges[i]}-{edges[i + 1]}",
        )
        for i, count in enumerate(buckets)
    ]


class TrimmedRows(NamedTuple):
    """The result of `trim_upper_outliers`."""

    rows: list
    excluded_count: int
    # The largest value kept, or None when nothing was trimmed. Reported so the
    # chart can say what it left out rather than silently dropping data.
    threshold: int | None


def _weighted_quantile(data_rows: Sequence, cumulative: Sequence[int], q: float):
    """Nearest-rank quantile over value/count rows sorted by value."""
    total = cumulative[-1]
    rank = max(1, math.ceil(q * total))
    idx = bisect.bisect_left(cumulative, rank)
    return data_rows[idx].value


# Below this many observations one long-running interview is not distinguishable
# from a tail, so nothing is trimmed.
_MIN_ROWS_FOR_TRIM = 20


def trim_upper_outliers(data_rows: Sequence) -> TrimmedRows:
    """Drop value/count rows above Tukey's upper fence (Q3 + 1.5 * IQR).

    A handful of interviews left open for hours stretch the duration axis over
    an order of magnitude the rest of the data never reaches, leaving one tall
    bar and a run of empty ones. Only the upper tail is trimmed: a very short
    interview is a real, readable observation.

    The rows are counts per distinct value, so the quartiles are taken over the
    observations they represent, not over the rows.
    """
    if not data_rows:
        return TrimmedRows([], 0, None)

    cumulative: list[int] = []
    running = 0
    for row in data_rows:
        running += row.count
        cumulative.append(running)

    total = cumulative[-1]
    if total < _MIN_ROWS_FOR_TRIM:
        return TrimmedRows(list(data_rows), 0, None)

    q1 = _weighted_quantile(data_rows, cumulative, 0.25)
    q3 = _weighted_quantile(data_rows, cumulative, 0.75)
    iqr = q3 - q1
    # A zero IQR means the middle half sits on a single value; the fence would
    # then collapse onto Q3 and cut away the entire upper half as "outliers".
    if iqr <= 0:
        return TrimmedRows(list(data_rows), 0, None)

    fence = q3 + 1.5 * iqr
    kept = [row for row in data_rows if row.value <= fence]
    excluded = total - sum(row.count for row in kept)
    if not kept or excluded == 0:
        return TrimmedRows(list(data_rows), 0, None)

    return TrimmedRows(kept, excluded, int(kept[-1].value))
