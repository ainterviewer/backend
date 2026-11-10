"""The report's card for a Likert grid: each row's ratings counted on the
scale, matched by row id and placed by position in the scale the respondent
saw."""

import json
import uuid

from ainterviewer.interview_guides import GridRow, LikertGridItem
from app.api.dashboard.analysis.report import _Answer, _grid_rows

SCALE = ["Very dissatisfied", "Dissatisfied", "Neutral", "Satisfied"]
ROWS = [
    GridRow(id=uuid.UUID(int=1), text="Cleanliness"),
    GridRow(id=uuid.UUID(int=2), text="Signage", required=False),
]


def _item() -> LikertGridItem:
    return LikertGridItem(options=SCALE, rows=[row.model_copy() for row in ROWS])


def _answer(item: LikertGridItem, values: dict, language: str = "EN") -> _Answer:
    return _Answer(
        content=item.encode_answer(values),
        language=language,
        options=item.options,
        item=item,
    )


def test_each_row_is_counted_on_the_scale():
    item = _item()
    danish = LikertGridItem(
        options=[f"{option} (DA)" for option in SCALE],
        rows=[row.model_copy(update={"text": row.text + " (DA)"}) for row in ROWS],
    )
    rows = _grid_rows(
        item,
        [
            _answer(item, {ROWS[0].id: "Satisfied", ROWS[1].id: "Neutral"}),
            _answer(item, {ROWS[0].id: "Satisfied"}),
            # Placed by position in the scale this respondent saw.
            _answer(danish, {ROWS[0].id: "Dissatisfied (DA)"}, language="DA"),
        ],
    )
    assert [row.label for row in rows] == ["Cleanliness", "Signage"]
    cleanliness, signage = rows
    assert cleanliness.n_answered == 3 and signage.n_answered == 1
    assert [bar.count for bar in cleanliness.counts] == [0, 1, 0, 2]
    assert cleanliness.counts[1].by_language == {"DA": 1}
    assert [bar.count for bar in signage.counts] == [0, 0, 1, 0]


def test_a_rating_for_a_removed_row_is_not_counted():
    item = _item()
    content = json.dumps({str(uuid.uuid4()): "Neutral"})
    rows = _grid_rows(item, [_Answer(content, "EN", SCALE, item)])
    assert all(row.n_answered == 0 for row in rows)
