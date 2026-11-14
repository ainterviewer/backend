"""Each user has their own guide-assistant conversation in each project.

The server picks the session -- the user's newest in the project -- so
opening the assistant in another project, or another user opening it in
this one, never reads or replaces this conversation. Starting a new chat
makes a newer session, which is then the current one.

Foreign keys are off on this bare engine, so the owners are bare ids.
"""

import uuid

import pytest
from pydantic_ai import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.messages import ModelMessagesTypeAdapter
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.repositories.assistance import AssistanceRepository
from app.db.tables import Base

PROJECT = uuid.uuid4()
OTHER_PROJECT = uuid.uuid4()
OWNER = uuid.uuid4()
OTHER_USER = uuid.uuid4()


@pytest.fixture
def repo():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield AssistanceRepository(session)


def test_a_user_without_a_session_has_none(repo):
    assert repo.current_session(PROJECT, OWNER) is None


def test_the_newest_session_is_the_current_one(repo):
    repo.create_new_session(PROJECT, OWNER)
    newest = repo.create_new_session(PROJECT, OWNER)

    assert repo.current_session(PROJECT, OWNER) == newest


def test_sessions_in_other_projects_or_of_other_users_are_left_alone(repo):
    mine = repo.create_new_session(PROJECT, OWNER)
    repo.create_new_session(OTHER_PROJECT, OWNER)
    repo.create_new_session(PROJECT, OTHER_USER)

    assert repo.current_session(PROJECT, OWNER) == mine


def test_messages_come_back_in_the_order_they_were_added(repo):
    session_id = repo.create_new_session(PROJECT, OWNER)
    for prompt, reply in (("My topic", "Tell me more."), ("Commuting", "Nice.")):
        repo.add_messages(
            ModelMessagesTypeAdapter.dump_json(
                [
                    ModelRequest(parts=[UserPromptPart(content=prompt)]),
                    ModelResponse(parts=[TextPart(reply)]),
                ]
            ),
            session_id=session_id,
        )

    texts = [part.content for m in repo.get_messages(session_id) for part in m.parts]
    assert texts == ["My topic", "Tell me more.", "Commuting", "Nice."]
