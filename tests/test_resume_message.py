"""A client that reconnects keeps its chat, so it is not sent the history, but
it is sent the message the interview is waiting on it to answer -- the only
way it can tell whose turn it is, or that its answer was lost in the drop."""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ainterviewer.interfaces import SecurityIntervention, SecurityOverride
from ainterviewer.interview_guides import InterviewGuide, TimedMessage
from ainterviewer.interview_guides.history import InterviewHistory
from ainterviewer.interview_guides.types import ConditionAction
from ainterviewer.lpm.types import CustomToken
from ainterviewer.types import MessageRole, MessageType
from app.api.websockets.interviews.replay import resume_message
from app.db.repositories.interview import InterviewRepository
from app.db.tables import Base, InterviewTable
from app.types import InterviewType

PROJECT = uuid.uuid4()
INTERVIEW = uuid.uuid4()

GUIDE = InterviewGuide.model_validate(
    {
        "framing": "Framing",
        "question_sections": [
            {"description": "Section", "questions": [{"main_question": "Why?"}]}
        ],
    }
)
QUESTION = {"section": 0, "main_question": 0}


@pytest.fixture
def interviews():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            InterviewTable(
                id=INTERVIEW,
                project_id=PROJECT,
                interview_guide=GUIDE,
                type=InterviewType.MANUAL_TEST,
            )
        )
        session.flush()
        yield InterviewRepository(session)


def resumed(interviews, *rows: tuple[MessageRole, str, dict], guide=GUIDE):
    for message_id, (role, content, fields) in enumerate(rows):
        interviews.insert_message(
            message_id=message_id,
            content=content,
            role=role,
            interview_id=INTERVIEW,
            project_id=PROJECT,
            **fields,
        )
    return resume_message(interviews.get_messages(INTERVIEW, PROJECT), guide)


def test_the_question_waiting_on_an_answer_is_resent(interviews):
    message = resumed(interviews, (MessageRole.ASSISTANT, "Why?", QUESTION))

    assert message is not None
    assert message.message_id == 0
    assert message.content == "Why?"
    assert message.can_answer


def test_nothing_while_the_answer_is_being_replied_to(interviews):
    # The interview goes on by itself, and its reply sets the turn.
    assert (
        resumed(
            interviews,
            (MessageRole.ASSISTANT, "Why?", QUESTION),
            (MessageRole.USER, "Because", QUESTION),
        )
        is None
    )


def test_nothing_for_a_message_that_takes_no_answer(interviews):
    assert (
        resumed(interviews, (MessageRole.ASSISTANT, "Hello", {"can_answer": False}))
        is None
    )


def test_nothing_for_a_timed_message(interviews):
    timed = TimedMessage(message="Halfway", time=10)
    guide = GUIDE.model_copy(update={"timed_messages": [timed]})
    message = resumed(
        interviews,
        (MessageRole.ASSISTANT, "Halfway", {"timed_message_id": timed.id}),
        guide=guide,
    )
    assert message is None


def test_nothing_after_the_end(interviews):
    assert (
        resumed(interviews, (MessageRole.ASSISTANT, CustomToken.end_of_interview, {}))
        is None
    )


def test_an_intervention_the_respondent_may_override_is_resent(interviews):
    intervention = SecurityIntervention(
        action=ConditionAction.SKIP_PROBES, respondent_override=True
    )
    message = resumed(
        interviews,
        (MessageRole.ASSISTANT, "Careful", {"security_intervention": intervention}),
    )
    assert message is not None
    assert message.security_intervention == intervention


def test_nothing_for_an_intervention_that_does_not_wait(interviews):
    intervention = SecurityIntervention(
        action=ConditionAction.SKIP_PROBES, respondent_override=False
    )
    assert (
        resumed(
            interviews,
            (MessageRole.ASSISTANT, "Careful", {"security_intervention": intervention}),
        )
        is None
    )


def test_nothing_once_the_intervention_was_answered(interviews):
    intervention = SecurityIntervention(
        action=ConditionAction.SKIP_PROBES, respondent_override=True
    )
    assert (
        resumed(
            interviews,
            (MessageRole.ASSISTANT, "Careful", {"security_intervention": intervention}),
            (
                MessageRole.USER,
                SecurityOverride.OVERRIDE,
                {"message_type": MessageType.SECURITY_OVERRIDE},
            ),
        )
        is None
    )


def test_a_resumed_history_knows_which_suggestion_a_probe_was_asked_from(
    interviews,
):
    # A reworded probe does not read as its suggestion: only the stored mark
    # keeps the resumed interview from asking it again.
    probe = {**QUESTION, "sub_question": 1}
    rows = [
        (MessageRole.ASSISTANT, "Why?", {**QUESTION, "sub_question": 0}),
        (MessageRole.USER, "Because", {**QUESTION, "sub_question": 0}),
        (
            MessageRole.ASSISTANT,
            "And when was that?",
            {**probe, "suggested_probe": "When?"},
        ),
        (MessageRole.USER, "Not sure", probe),
    ]
    for message_id, (role, content, fields) in enumerate(rows):
        interviews.insert_message(
            message_id=message_id,
            content=content,
            role=role,
            interview_id=INTERVIEW,
            project_id=PROJECT,
            **fields,
        )

    history = InterviewHistory()
    history.process_history(interviews.get_messages(INTERVIEW, PROJECT), GUIDE)

    (turn,) = history[0][0].probes
    assert turn.question.message == "And when was that?"
    assert turn.suggested_probe == "When?"
