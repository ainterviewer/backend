"""Tests that a message sent by the interview's security check keeps its
marker through the database, so a resumed interview replays it in a modal
rather than as a chat message."""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ainterviewer.interfaces import (
    OutgoingData,
    OutgoingHistoryMessage,
    OutgoingMessage,
    SecurityIntervention,
    SecurityOverride,
)
from ainterviewer.interview_guides import InterviewGuide
from ainterviewer.interview_guides.types import ConditionAction
from ainterviewer.types import MessageRole, MessageType
from app.db.repositories.interview import InterviewRepository
from app.db.tables import Base, InterviewTable
from app.db.types import InterviewType
from app.utils import replay_history

PROJECT = uuid.uuid4()
INTERVIEW = uuid.uuid4()

INTERVENTION = SecurityIntervention(
    action=ConditionAction.SKIP_PROBES, respondent_override=True
)


@pytest.fixture
def interviews():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        # Messages read back carry their interview's type.
        session.add(
            InterviewTable(
                id=INTERVIEW,
                project_id=PROJECT,
                interview_guide=InterviewGuide(),
                type=InterviewType.MANUAL_TEST,
            )
        )
        session.flush()
        yield InterviewRepository(session)


def insert(
    interviews: InterviewRepository,
    message_id: int,
    role: MessageRole,
    security_intervention: SecurityIntervention | None = None,
) -> None:
    interviews.insert_message(
        message_id=message_id,
        content=f"Message {message_id}",
        role=role,
        interview_id=INTERVIEW,
        project_id=PROJECT,
        security_intervention=security_intervention,
    )


def message_ids(
    replayed: list[OutgoingHistoryMessage | OutgoingData | OutgoingMessage],
) -> list[int]:
    """The ids of the replayed messages, none of which may be a bare data frame."""
    ids = []
    for message in replayed:
        assert not isinstance(message, OutgoingData)
        ids.append(message.message_id)
    return ids


def insert_override(interviews: InterviewRepository, message_id: int) -> None:
    """The respondent's answer to an intervention, as the interviewer stores it."""
    interviews.insert_message(
        message_id=message_id,
        content=SecurityOverride.OVERRIDE,
        role=MessageRole.USER,
        message_type=MessageType.SECURITY_OVERRIDE,
        include_in_history=False,
        interview_id=INTERVIEW,
        project_id=PROJECT,
    )


def test_marker_is_stored(interviews):
    insert(interviews, 0, MessageRole.ASSISTANT, INTERVENTION)
    insert(interviews, 1, MessageRole.ASSISTANT)

    intervention, question = interviews.get_messages(INTERVIEW, PROJECT)

    assert intervention.security_intervention == INTERVENTION
    assert question.security_intervention is None


def test_marker_is_replayed_in_history(interviews):
    insert(interviews, 0, MessageRole.ASSISTANT, INTERVENTION)
    insert(interviews, 1, MessageRole.ASSISTANT)

    replayed, _ = replay_history(
        interviews.get_messages(INTERVIEW, PROJECT), PROJECT, INTERVIEW
    )

    intervention, question = replayed
    assert isinstance(intervention, OutgoingHistoryMessage)
    assert intervention.security_intervention == INTERVENTION
    assert isinstance(question, OutgoingMessage)
    assert question.security_intervention is None


def test_marker_is_replayed_on_last_message(interviews):
    # An intervention still waiting on the respondent when they reconnect.
    insert(interviews, 0, MessageRole.USER)
    insert(interviews, 1, MessageRole.ASSISTANT, INTERVENTION)

    replayed, _ = replay_history(
        interviews.get_messages(INTERVIEW, PROJECT), PROJECT, INTERVIEW
    )

    assert isinstance(replayed[-1], OutgoingMessage)
    assert replayed[-1].security_intervention == INTERVENTION


def test_override_answer_is_stored(interviews):
    insert(interviews, 0, MessageRole.ASSISTANT, INTERVENTION)
    insert_override(interviews, 1)

    _, override = interviews.get_messages(INTERVIEW, PROJECT)

    assert override.message_type == MessageType.SECURITY_OVERRIDE
    assert override.content == SecurityOverride.OVERRIDE


def test_override_answer_is_not_replayed(interviews):
    # Given in the modal, so it has no place in the chat.
    insert(interviews, 0, MessageRole.ASSISTANT, INTERVENTION)
    insert_override(interviews, 1)
    insert(interviews, 2, MessageRole.ASSISTANT)

    replayed, _ = replay_history(
        interviews.get_messages(INTERVIEW, PROJECT), PROJECT, INTERVIEW
    )

    assert message_ids(replayed) == [0, 2]


def test_override_answer_is_not_replayed_last(interviews):
    insert(interviews, 0, MessageRole.ASSISTANT, INTERVENTION)
    insert_override(interviews, 1)

    replayed, _ = replay_history(
        interviews.get_messages(INTERVIEW, PROJECT), PROJECT, INTERVIEW
    )

    assert message_ids(replayed) == [0]
