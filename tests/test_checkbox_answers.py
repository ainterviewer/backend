"""Checkbox answers whose options hold commas: the answer is the choices
joined with ", ", so it is split by the item's own options, not on every
comma."""

import uuid

from ainterviewer.interview_guides.survey_items import CheckboxItem
from app.db.survey_answers import AnswerRow, OptionValue, TextValue, values_of


def _checkbox(content: str, options: list[str]) -> AnswerRow:
    return AnswerRow(
        interview_id=uuid.UUID(int=0),
        coordinate=(0, 0),
        content=content,
        item=CheckboxItem(options=options, with_other=True),
        language="EN",
    )


def test_a_checkbox_option_with_a_comma_is_one_value():
    options = ["Yes, often", "Yes, rarely", "No"]
    assert values_of(_checkbox("Yes, often, No", options)) == [
        OptionValue(0),
        OptionValue(2),
    ]


def test_a_write_in_with_commas_is_one_value():
    assert values_of(_checkbox("No, red, green", ["Yes", "No"])) == [
        OptionValue(1),
        TextValue("red, green"),
    ]
