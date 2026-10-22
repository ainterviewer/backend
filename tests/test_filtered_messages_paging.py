"""The coding page's message list pages in a fixed order.

The page asks for its next page by how many messages it already holds, so
consecutive pages have to tile one ordering: without an `ORDER BY` the
database is free to return rows in a different order per query, and "load
more" can repeat some messages and never show others.

Runs against an in-memory SQLite database.
"""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ainterviewer.interview_guides import InterviewGuide
from ainterviewer.types import MessageRole
from app.db.repositories.analysis import AnalysisRepository
from app.db.tables import (
    Base,
    InterviewTable,
    MessageTable,
    ProjectFolderTable,
    ProjectTable,
)
from app.types import InterviewType

N_INTERVIEWS = 4
N_MESSAGES = 7


@pytest.fixture
def seeded():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        folder = ProjectFolderTable(title="Folder")
        project = ProjectTable(folder=folder, title="Project", owner_id=uuid.uuid4())
        session.add_all([folder, project])
        session.flush()
        for _ in range(N_INTERVIEWS):
            interview = InterviewTable(
                project=project,
                interview_guide=InterviewGuide(),
                type=InterviewType.DISTRIBUTED,
            )
            session.add(interview)
            session.flush()
            # Inserted out of order, so insertion order cannot pass for the
            # ordering under test.
            for number in reversed(range(1, N_MESSAGES + 1)):
                session.add(
                    MessageTable(
                        message_id=number,
                        interview_id=interview.id,
                        project_id=project.id,
                        role=MessageRole.USER,
                        content=f"Answer {number}",
                    )
                )
        session.flush()
        yield AnalysisRepository(session), project.id


def coordinates(messages):
    return [(m.interview_id, m.message_id) for m in messages]


def test_messages_come_in_interview_then_message_order(seeded):
    analysis, project_id = seeded

    messages = analysis.get_filtered_messages(project_id, offset=0, limit=100)

    assert len(messages) == N_INTERVIEWS * N_MESSAGES
    assert coordinates(messages) == sorted(coordinates(messages))


def test_pages_tile_the_whole_list(seeded):
    """What "load more" does: ask from however many it already holds."""
    analysis, project_id = seeded
    everything = analysis.get_filtered_messages(project_id, offset=0, limit=100)

    paged = []
    while page := analysis.get_filtered_messages(
        project_id, offset=len(paged), limit=5
    ):
        paged.extend(page)

    assert [m.id for m in paged] == [m.id for m in everything]
