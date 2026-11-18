"""The question a resumed interview is waiting on is replayed live, and has to
say whether it can be skipped -- the client offers Skip from that flag alone,
and the message row does not store it."""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ainterviewer.interfaces import OutgoingMessage
from ainterviewer.interview_guides import InterviewGuide, TimedMessage
from ainterviewer.types import MessageRole
from app.api.websockets.interviews.replay import replay_history
from app.db.repositories.interview import InterviewRepository
from app.db.tables import Base, InterviewTable
from app.types import InterviewType

PROJECT = uuid.uuid4()
INTERVIEW = uuid.uuid4()

GUIDE = InterviewGuide.model_validate(
    {
        "framing": "Framing",
        "question_sections": [
            {
                "description": "Section",
                "questions": [
                    {"main_question": "Skippable?"},
                    {"main_question": "Required?", "can_skip": False},
                ],
            }
        ],
    }
)


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


def replay_waiting_on(
    interviews: InterviewRepository, question: int
) -> OutgoingMessage:
    for message_id, role in enumerate([MessageRole.ASSISTANT, MessageRole.USER]):
        interviews.insert_message(
            message_id=message_id,
            content=f"Message {message_id}",
            role=role,
            section=0,
            main_question=0,
            interview_id=INTERVIEW,
            project_id=PROJECT,
        )
    interviews.insert_message(
        message_id=2,
        content="Waiting",
        role=MessageRole.ASSISTANT,
        section=0,
        main_question=question,
        interview_id=INTERVIEW,
        project_id=PROJECT,
    )
    replayed, _ = replay_history(
        interviews.get_messages(INTERVIEW, PROJECT), PROJECT, GUIDE
    )
    last = replayed[-1]
    assert isinstance(last, OutgoingMessage)
    return last


def test_unskippable_question_is_replayed_unskippable(interviews):
    assert replay_waiting_on(interviews, 1).can_skip is False


def test_skippable_question_is_replayed_skippable(interviews):
    assert replay_waiting_on(interviews, 0).can_skip is True


# -- Timed messages shown in a modal --------------------------------------------


def _stored(interviews: InterviewRepository, *rows: tuple[str, dict]):
    for message_id, (content, fields) in enumerate(rows):
        interviews.insert_message(
            message_id=message_id,
            content=content,
            role=MessageRole.ASSISTANT,
            interview_id=INTERVIEW,
            project_id=PROJECT,
            **fields,
        )
    return interviews.get_messages(INTERVIEW, PROJECT)


def test_a_timed_message_shown_in_a_modal_is_not_replayed_into_the_chat(interviews):
    modal = TimedMessage(message="Halfway", time=10, as_modal=True)
    plain = TimedMessage(message="Almost done", time=20)
    guide = GUIDE.model_copy(update={"timed_messages": [modal, plain]})
    rows = _stored(
        interviews,
        ("Halfway", {"timed_message_id": modal.id}),
        ("Almost done", {"timed_message_id": plain.id}),
        ("Skippable?", {"section": 0, "main_question": 0}),
    )

    replayed, _ = replay_history(rows, PROJECT, guide)

    assert [message.content for message in replayed] == ["Almost done", "Skippable?"]


def test_one_waited_on_goes_out_live_as_a_modal(interviews):
    modal = TimedMessage(message="Halfway", time=10, as_modal=True)
    guide = GUIDE.model_copy(update={"timed_messages": [modal]})
    rows = _stored(interviews, ("Halfway", {"timed_message_id": modal.id}))

    (last,), _ = replay_history(rows, PROJECT, guide)

    assert isinstance(last, OutgoingMessage)
    assert last.as_modal
