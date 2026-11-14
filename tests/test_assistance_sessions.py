"""The guide assistant's chat sessions stay with the user who started them.

The session id travels in a plain JSON cookie the client can rewrite, and the
route's role check proves only that the caller edits the project in the URL.
So the repository answers for a session only to its owner in its project: a
session id copied from someone else opens nothing.

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
INTRUDER = uuid.uuid4()


@pytest.fixture
def repo():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield AssistanceRepository(session)


@pytest.fixture
def session_id(repo):
    session_id = repo.create_new_session(PROJECT, OWNER)
    repo.add_messages(
        ModelMessagesTypeAdapter.dump_json(
            [
                ModelRequest(parts=[UserPromptPart(content="My research topic")]),
                ModelResponse(parts=[TextPart("Tell me more.")]),
            ]
        ),
        session_id=session_id,
    )
    return session_id


def test_owner_gets_their_session_and_history(repo, session_id):
    assert repo.owned_session(session_id, PROJECT, OWNER) == session_id
    assert len(repo.get_messages(session_id, PROJECT, OWNER)) == 2


@pytest.mark.parametrize(
    ("project_id", "user_id"),
    [(PROJECT, INTRUDER), (OTHER_PROJECT, OWNER), (OTHER_PROJECT, INTRUDER)],
    ids=["other user", "other project", "both"],
)
def test_session_is_closed_to_anyone_else(repo, session_id, project_id, user_id):
    assert repo.owned_session(session_id, project_id, user_id) is None
    assert repo.get_messages(session_id, project_id, user_id) == []


def test_unknown_session_is_none(repo):
    assert repo.owned_session(uuid.uuid4(), PROJECT, OWNER) is None
