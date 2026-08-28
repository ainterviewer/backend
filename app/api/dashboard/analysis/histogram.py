"""Histogram binning shared by the analysis endpoints.

Lives in its own module because both `monitoring` (interview-level histograms)
and `report` (per-item histograms) bin value/count rows the same way,
and an axis that reads 300, 350, 400 in one place should read the same in the
other.
"""

import math
from collections.abc import Sequence

from pydantic import BaseModel


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
            HistogramBucket(value=int(min_val), count=total_count, label=str(min_val))
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
