"""Starting a test interview part-way through its guide.

The interview loop reads answers by position, so a test that starts in the
middle runs a copy of the guide cut down to what comes from the start point
on. These pin what the cut keeps, and that nothing left behind can reach for
an answer that was never collected.
"""

from uuid import uuid4

import pytest

from ainterviewer.interview_guides.conditions import (
    Condition,
    ConditionEvaluation,
    Conditions,
    QuestionContext,
)
from ainterviewer.interview_guides.interview_guide import InterviewGuide
from ainterviewer.interview_guides.questions import Question
from ainterviewer.interview_guides.references import Reference
from ainterviewer.interview_guides.sections import QuestionSection
from ainterviewer.interview_guides.types import ConditionAction
from app.services.guide_start import MISSING_ANSWER, StartPointError, start_guide_at


def _condition(question: Question) -> Conditions:
    return Conditions(
        action=ConditionAction.SKIP_QUESTION,
        conditions=[
            Condition(
                question_context=QuestionContext(
                    question_index=(0, 0), question_id=question.id
                ),
                evaluation=[ConditionEvaluation(trigger_value="no")],
            )
        ],
    )


def _guide() -> InterviewGuide:
    """Two sections of three questions; the last two read earlier answers."""
    a = [Question(main_question=f"a{n}") for n in range(3)]
    b = [Question(main_question=f"b{n}") for n in range(3)]

    # Reads a question in the first section.
    b[1].conditions = _condition(a[0])
    # Reads a question earlier in its own section.
    b[2].conditions = _condition(b[0])
    b[2].references = [Reference(question_index=(0, 0), question_id=a[1].id)]
    b[2].main_question = "You said {} earlier?"

    return InterviewGuide(
        introduction="Welcome!",
        question_sections=[
            QuestionSection(description="A", questions=a),
            QuestionSection(description="B", questions=b),
        ],
    )


def _texts(guide: InterviewGuide) -> list[list[str]]:
    return [[q.main_question for q in s.questions] for s in guide.question_sections]


def test_starting_at_a_section_drops_the_ones_before_it():
    guide = _guide()
    section = guide.question_sections[1]

    first = start_guide_at(guide, section_id=section.id)

    assert first == section.questions[0].id
    assert [s.description for s in guide.question_sections] == ["B"]
    assert guide.introduction == ""


def test_starting_at_a_question_drops_the_ones_before_it_in_its_section():
    guide = _guide()
    question = guide.question_sections[0].questions[1]

    first = start_guide_at(guide, question_id=question.id)

    assert first == question.id
    assert _texts(guide)[0] == ["a1", "a2"]
    assert len(guide.question_sections) == 2


def test_a_rule_reading_a_cut_question_is_dropped():
    guide = _guide()

    start_guide_at(guide, section_id=guide.question_sections[1].id)

    _, b1, b2 = guide.question_sections[0].questions
    # b1 read a0, which was cut; b2 reads b0, which was kept.
    assert b1.conditions is None
    assert b2.conditions is not None
    assert b2.conditions.conditions[0].question_context.question_index == (0, 0)


def test_a_reference_to_a_cut_question_is_spelled_out():
    guide = _guide()

    start_guide_at(guide, section_id=guide.question_sections[1].id)

    b2 = guide.question_sections[0].questions[2]
    assert b2.references is None
    assert b2.main_question == f"You said {MISSING_ANSWER} earlier?"


def test_what_is_kept_still_validates():
    """The interview's copy is stored and read back, which re-runs the
    guide's reference checks: nothing may point at a cut question."""
    guide = _guide()
    start_guide_at(guide, question_id=guide.question_sections[1].questions[2].id)

    InterviewGuide.model_validate(guide.model_dump())


def test_the_start_stays_first_through_a_shuffle():
    guide = _guide()
    for section in guide.question_sections:
        section.shuffle = True
        for question in section.questions:
            question.shuffle = True
    start = guide.question_sections[0].questions[1]

    start_guide_at(guide, question_id=start.id)
    for _ in range(20):
        guide.shuffle()
        assert guide.question_sections[0].questions[0].id == start.id


def test_a_start_point_must_be_in_the_guide():
    with pytest.raises(StartPointError):
        start_guide_at(_guide(), question_id=uuid4())


def test_exactly_one_start_point():
    guide = _guide()
    section = guide.question_sections[0]
    with pytest.raises(StartPointError):
        start_guide_at(guide)
    with pytest.raises(StartPointError):
        start_guide_at(
            guide, section_id=section.id, question_id=section.questions[0].id
        )


def test_an_empty_section_has_nothing_to_start_at():
    guide = _guide()
    guide.question_sections.append(QuestionSection(description="C", questions=[]))
    with pytest.raises(StartPointError):
        start_guide_at(guide, section_id=guide.question_sections[2].id)
