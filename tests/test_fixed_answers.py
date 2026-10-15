"""Fixed answers are matched to the question asked, by its id.

They used to be a list spent in the order questions came, so a shuffled
section, a condition that skipped a question or a reordered guide put every
later answer on another question.
"""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ainterviewer.interfaces import OutgoingMessage
from ainterviewer.lpm.types import CustomToken
from ainterviewer.types import MessageRole, TestType
from app.db import InterviewDataBase
from app.db.repositories.errors import ContentRevisionError
from app.db.tables import Base, TestSetupTable
from app.synthesize import core

pytestmark = pytest.mark.anyio

FIRST, SECOND, UNANSWERED = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def _message(question_id: uuid.UUID | None, *, is_probe: bool = False) -> dict:
    return OutgoingMessage(
        content="?",
        role=MessageRole.ASSISTANT,
        message_id=1,
        question_id=question_id,
        is_probe=is_probe,
    ).model_dump()


async def _replies(monkeypatch, messages: list[dict], answers: dict) -> list[str]:
    sent: list[str] = []

    class _Token:
        def encode(self) -> str:
            return "token"

    async def fetch_token(**_kwargs):
        return _Token()

    async def connect(_token):
        for message in messages:
            yield message, None

    async def send(_websocket, text, _delay=None):
        sent.append(text)

    monkeypatch.setattr(core, "fetch_token", fetch_token)
    monkeypatch.setattr(core, "_connect_and_yield_messages", connect)
    monkeypatch.setattr(core, "_send_response", send)
    await core.run_synthetic_fixed_answers(
        user_token="u", project_id="p", test_run_id="r", fixed_answers=answers
    )
    return sent


async def test_each_question_gets_its_own_answer_in_any_order(monkeypatch):
    # Asked in the opposite order to the guide, as a shuffled section would.
    sent = await _replies(
        monkeypatch,
        [_message(SECOND), _message(FIRST)],
        {FIRST: "first", SECOND: "second"},
    )
    assert sent == ["second", "first"]


async def test_probes_and_questions_without_an_answer_are_skipped(monkeypatch):
    sent = await _replies(
        monkeypatch,
        [_message(FIRST), _message(FIRST, is_probe=True), _message(UNANSWERED)],
        {FIRST: "first"},
    )
    assert sent == ["first", CustomToken.skip_question, CustomToken.skip_question]


def test_blank_answers_are_left_out():
    # Imported here: importing it before `app.db` trips an import cycle.
    from app.api.request_models import UpdateFixedAnswersRequest

    request = UpdateFixedAnswersRequest(answers={FIRST: "  kept  ", SECOND: "   "})
    assert request.answers == {FIRST: "kept"}


def test_answers_round_trip_and_a_stale_save_is_refused():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        setup = TestSetupTable(
            project_id=uuid.uuid4(),
            type=TestType.FIXED_ANSWERS,
            answering_model="m",
        )
        session.add(setup)
        session.commit()
        db = InterviewDataBase(session)

        assert db.tests.update_fixed_answers(setup.id, {FIRST: "first"}, 1) == 2

        assert db.tests.get_fixed_answers(setup.id) == {FIRST: "first"}

        # A save based on the revision before it: someone else saved since.
        with pytest.raises(ContentRevisionError) as stale:
            db.tests.update_fixed_answers(setup.id, {FIRST: "theirs"}, 1)
        assert stale.value.current == 2
        assert db.tests.get_fixed_answers(setup.id) == {FIRST: "first"}
