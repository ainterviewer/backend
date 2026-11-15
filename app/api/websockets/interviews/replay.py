"""Replaying a stored interview to a respondent who reconnects."""

from pydantic import UUID4

from ainterviewer.interfaces import (
    OutgoingData,
    OutgoingHistoryMessage,
    OutgoingMessage,
)
from ainterviewer.interview_guides import InterviewGuide
from ainterviewer.lpm.types import CustomToken
from ainterviewer.types import MessageType

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
        if (
            message.skipped_by_condition
            or message.message_type == MessageType.SECURITY_OVERRIDE
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
            if last_message.image:
                last_message.image.encode(project_id)  # ty:ignore[missing-union-attribute]

            data = OutgoingMessage(
                content=last_message.content,
                role=last_message.role,
                message_id=last_message.message_id,
                can_answer=last_message.can_answer,
                can_skip=_can_skip(last_message, interview_guide),
                feedback=last_message.feedback,
                image=last_message.image,
                survey_item=last_message.survey_item,
                security_intervention=last_message.security_intervention,
            )
            messages.append(data)

    return messages, continue_from_history
