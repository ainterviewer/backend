"""Start a test interview part-way through its guide.

The interview loop reads answers back by position -- the n-th section of the
history is the n-th section of the guide -- so it cannot simply jump ahead.
Instead the interview's own copy of the guide is cut down to what comes from
the start point on, and runs from its beginning like any other.

What is cut was never asked, so nothing can read its answers:

- A rule (a question's `Conditions`) reading a cut question is dropped whole.
  Its answer would have been unknown, and an unknown term leaves a rule
  undecided -- so it would not have fired -- except in a chain like
  `cut OR other`, where `other` alone could decide it. Dropping the rule is
  the simple, predictable reading: a test from the middle never skips or ends
  on an answer it did not collect.
- A reference to a cut question has no answer to quote, and `ask_question`
  refuses to ask a question whose referenced answer is missing. The question
  keeps its text with the placeholder spelled out instead.
"""

from uuid import UUID

from ainterviewer.interview_guides.interview_guide import InterviewGuide

#: Stands in for the `{}` a reference fills, when its answer was cut.
MISSING_ANSWER = "[an earlier answer]"


class StartPointError(ValueError):
    """The start point is not in the guide, or there is nothing to ask from it."""


def start_guide_at(
    guide: InterviewGuide,
    *,
    section_id: UUID | None = None,
    question_id: UUID | None = None,
) -> UUID:
    """Cut `guide` down, in place, to the questions from the start point on.

    Starting at a section starts at its first question; starting at a question
    also drops the questions before it in its section. Positions are the
    authored ones, so this must run *before* `shuffle`. The start section and
    question are pinned so shuffling keeps them first.

    The introduction is dropped too: it greets a respondent at the start of
    the interview, and this one starts in the middle.

    Returns the id of the question the interview now starts at.
    """
    if (section_id is None) == (question_id is None):
        raise StartPointError("Give a section or a question to start at, not both")

    sections = guide.question_sections
    for n_section, section in enumerate(sections):
        if section.id == section_id:
            n_question = 0
            break
        positions = [question.id for question in section.questions]
        if question_id is not None and question_id in positions:
            n_question = positions.index(question_id)
            break
    else:
        raise StartPointError("The start point is not in this guide")

    start = sections[n_section]
    if not start.questions:
        raise StartPointError("The section to start at has no questions")

    start.questions = start.questions[n_question:]
    guide.question_sections = sections[n_section:]

    start.shuffle = False
    start.questions[0].shuffle = False

    kept = {
        question.id
        for section in guide.question_sections
        for question in section.questions
    }
    for section in guide.question_sections:
        for question in section.questions:
            if question.conditions and any(
                condition.question_context.question_id not in kept
                for condition in question.conditions.conditions
            ):
                question.conditions = None

            if question.references and any(
                reference.question_id not in kept for reference in question.references
            ):
                question.references = None
                question.main_question = question.main_question.replace(
                    "{}", MISSING_ANSWER
                )

    guide.introduction = ""
    guide.sync_question_refs()
    return start.questions[0].id
