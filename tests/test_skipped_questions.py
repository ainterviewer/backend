"""A guide saved from the doc while some of its questions have no text yet.

The draft is saved without them, so the rest can be tried while they are
written one at a time; they still hold back publishing. What reads one of
them -- a condition, a reference -- cannot do without it, so the guide is
not saved at all.
"""

from ainterviewer.interview_guides.conditions import (
    Condition,
    ConditionBlock,
    ConditionEvaluation,
    QuestionContext,
)
from ainterviewer.interview_guides.interview_guide import InterviewGuide
from ainterviewer.interview_guides.localized import LocalizedInterviewGuide
from ainterviewer.interview_guides.questions import Question
from ainterviewer.interview_guides.references import Reference
from ainterviewer.interview_guides.sections import QuestionSection
from ainterviewer.interview_guides.types import ConditionAction
from app.collab.parts import GUIDE
from app.types import blocking


def _condition(question: Question) -> Condition:
    return Condition(
        action=ConditionAction.SKIP_QUESTION,
        blocks=[
            ConditionBlock(
                question_context=QuestionContext(
                    question_index=(0, 0), question_id=question.id
                ),
                evaluation=[ConditionEvaluation(trigger_value="no")],
            )
        ],
    )


def _data(a: list[Question], b: list[Question]) -> dict:
    rendered = InterviewGuide(
        question_sections=[
            QuestionSection(description="A", questions=a),
            QuestionSection(description="B", questions=b),
        ],
    )
    guide = LocalizedInterviewGuide.from_rendered({"EN": rendered}, structure="EN")
    return guide.model_dump(mode="json")


def _ids(guide) -> list[list[str]]:
    return [[str(q.id) for q in s.questions] for s in guide.question_sections]


def test_a_question_with_no_text_is_left_out_of_the_draft():
    a = [Question(main_question="a0"), Question(main_question=" ")]
    b = [Question(main_question="b0"), Question(main_question="")]
    b[0].conditions = [_condition(a[0])]

    guide, errors = GUIDE.validate(_data(a, b), "EN")

    assert guide is not None
    assert _ids(guide) == [[str(a[0].id)], [str(b[0].id)]]
    assert [(e.message, e.skipped) for e in errors] == [
        ("Question 1.2 has no text.", True),
        ("Question 2.2 has no text.", True),
    ]
    assert blocking(errors) == []


def test_a_condition_on_the_question_left_out_goes_with_it():
    a = [Question(main_question="a0"), Question(main_question="")]
    a[1].conditions = [_condition(a[0])]

    guide, errors = GUIDE.validate(_data(a, [Question(main_question="b0")]), "EN")

    assert guide is not None
    assert _ids(guide)[0] == [str(a[0].id)]
    assert blocking(errors) == []


def test_positions_follow_the_questions_moved_up():
    a = [Question(main_question=""), Question(main_question="a1")]
    b = [Question(main_question="b0")]
    b[0].conditions = [_condition(a[1])]

    guide, _errors = GUIDE.validate(_data(a, b), "EN")

    assert isinstance(guide, LocalizedInterviewGuide)
    rendered = guide.render("EN")
    block = rendered.question_sections[1].questions[0].conditions[0].blocks[0]
    assert block.question_context.question_index == (0, 0)


def test_what_reads_a_question_with_no_text_keeps_the_guide_from_being_saved():
    a = [Question(main_question="a0"), Question(main_question="")]
    b = [Question(main_question="You said {}?"), Question(main_question="b1")]
    b[0].references = [Reference(question_index=(0, 1), question_id=a[1].id)]
    b[1].conditions = [_condition(a[1]), _condition(a[1])]

    guide, errors = GUIDE.validate(_data(a, b), "EN")

    assert guide is None
    assert [(e.message, e.field, e.skipped) for e in errors] == [
        (
            "Question 2.1 refers to the answer to question 1.2, which has no text yet.",
            "references",
            False,
        ),
        (
            "Question 2.2 has a condition on question 1.2, which has no text yet.",
            "conditions",
            False,
        ),
        ("Question 1.2 has no text.", "main_question", True),
    ]
    assert errors[1].fix == "Write question 1.2, or change the condition."
