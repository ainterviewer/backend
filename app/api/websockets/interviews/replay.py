"""Replaying a stored interview to a respondent who reconnects."""

from pydantic import UUID4

from ainterviewer.interfaces import (
    OutgoingData,
    OutgoingHistoryMessage,
    OutgoingMessage,
)
from ainterviewer.interview_guides import InterviewGuide
from ainterviewer.lpm.types import CustomToken
from ainterviewer.types import MessageRole, MessageType

from ....schemas.interviews import MessagePublic


def _can_skip(message: MessagePublic, interview_guide: InterviewGuide | None) -> bool:
    """Whether the guide lets the respondent skip the question `message` asks.

    Not stored on the message, so read off the interview's own guide, whose
    positions are the ones the message was asked at. Anything that is not a
    guide question (or a guide it cannot be found in) keeps the default.
    """
    if (
        interview_guide is None
        or message.section is None
        or message.main_question is None
    ):
        return True
    try:
        section = interview_guide.question_sections[message.section]
        return section.questions[message.main_question].can_skip
    except IndexError:
        return True


def _as_modal(message: MessagePublic, interview_guide: InterviewGuide | None) -> bool:
    """Whether `message` is a timed message the guide shows in a modal.

    Not stored on the message, so read off the interview's own guide by the
    timed message's id.
    """
    if interview_guide is None or message.timed_message_id is None:
        return False
    return any(
        timed.id == message.timed_message_id and timed.as_modal
        for timed in interview_guide.timed_messages or []
    )


def replay_history(
    interview_history: list[MessagePublic],
    project_id: UUID4,
    interview_guide: InterviewGuide | None = None,
) -> tuple[list[OutgoingHistoryMessage | OutgoingData | OutgoingMessage], bool]:
    """Replays the messages from the history through the websocket. Returns
    True if the interview should continue or False if it has reached the end
    already.

    The last message, the one the interview is waiting on, goes out live, so it
    carries the question's `can_skip` from `interview_guide`."""
    # TODO: Send images from history as well
    # NOTE: Should this be moved to the lib?

    messages: list[OutgoingHistoryMessage | OutgoingData | OutgoingMessage] = []
    continue_from_history = True

    # A survey question goes out with its item, and its answer as stored
    # (a grid's JSON included): the client pairs the two and draws the
    # answered item read-only, the way it looked when it was answered.
    for message in interview_history[:-1]:
        # NOTE: Why did we skip these in the first place?
        # if message.content in CustomTokens:
        #     continue

        # An override answer was given in the intervention's modal, never in
        # the chat.
        # A timed message shown in a modal never joined the chat, and the
        # respondent closed it already.
        if (
            message.skipped_by_condition
            or message.message_type == MessageType.SECURITY_OVERRIDE
            or _as_modal(message, interview_guide)
        ):
            continue

        if message.image:
            message.image.encode(project_id)  # ty:ignore[missing-union-attribute]

        data = OutgoingHistoryMessage(
            content=message.content,
            role=message.role,
            message_id=message.message_id,
            feedback=message.feedback,
            image=message.image,
            survey_item=message.survey_item,
            security_intervention=message.security_intervention,
        )

        messages.append(data)

    if (last_message := interview_history[-1]).message_type == (
        MessageType.SECURITY_OVERRIDE
    ):
        pass
    elif last_message.role == "user":
        data = OutgoingHistoryMessage(
            content=last_message.content,
            role=last_message.role,
            message_id=last_message.message_id,
        )
        messages.append(data)
    else:
        if last_message.content == CustomToken.end_of_interview:
            data = OutgoingData(
                content=CustomToken.end_of_interview,
            )
            messages.append(data)
            continue_from_history = False
        elif last_message.content in CustomToken:
            pass
        else:
            messages.append(_live(last_message, project_id, interview_guide))

    return messages, continue_from_history


def _live(
    message: MessagePublic,
    project_id: UUID4,
    interview_guide: InterviewGuide | None,
) -> OutgoingMessage:
    """`message` as it went out when it was first sent."""
    if message.image:
        message.image.encode(project_id)  # ty:ignore[missing-union-attribute]

    return OutgoingMessage(
        content=message.content,
        role=message.role,
        message_id=message.message_id,
        can_answer=message.can_answer,
        can_skip=_can_skip(message, interview_guide),
        feedback=message.feedback,
        image=message.image,
        survey_item=message.survey_item,
        security_intervention=message.security_intervention,
        as_modal=_as_modal(message, interview_guide),
    )


def resume_message(
    interview_history: list[MessagePublic],
    project_id: UUID4,
    interview_guide: InterviewGuide | None = None,
) -> OutgoingMessage | None:
    """What a client that reconnects with the chat still on screen is sent:
    the message the interview is waiting on the respondent to answer, or
    nothing when it is not waiting on them.

    The history is not replayed to such a client, so this is how it learns
    whose turn it is. A question it missed during the drop shows up; one it
    already has tells it that its answer never arrived, and to take one
    again. When the interview is not waiting, it goes on by itself and what
    it sends next sets the turn."""
    if not interview_history:
        return None
    last = interview_history[-1]

    if (
        last.role != MessageRole.ASSISTANT
        or last.message_type == MessageType.SECURITY_OVERRIDE
        or last.skipped_by_condition
        or last.content in CustomToken
        or _as_modal(last, interview_guide)
    ):
        return None

    if last.security_intervention is not None:
        # Waits only on one the respondent may override, and has not.
        waiting = last.security_intervention.respondent_override
    else:
        # Outside the guide's structure (a timed message) or taking no
        # answer, the interview moves on without the respondent.
        waiting = (
            last.can_answer
            and last.section is not None
            and last.main_question is not None
        )

    return _live(last, project_id, interview_guide) if waiting else None
