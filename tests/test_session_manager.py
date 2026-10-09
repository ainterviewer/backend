"""Tests for ending live interview sessions before their interviews are deleted.

Plain asyncio tasks stand in for websocket sessions: no server, no database.
"""

import asyncio
from uuid import UUID, uuid4

from app.api.websockets.manager import InterviewSessionManager


async def _hold(
    manager: InterviewSessionManager,
    project_id: UUID,
    interview_id: UUID,
    outcome: dict[UUID, str],
    started: asyncio.Event,
) -> None:
    """A session as `ai.py` runs one: claim, work until cancelled, release."""
    done = await manager.claim(project_id, interview_id)
    started.set()
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        deleted = manager.ended_by_delete(project_id, interview_id, done)
        outcome[interview_id] = "deleted" if deleted else "replaced"
    finally:
        manager.release(project_id, interview_id, done)


async def _start(
    manager: InterviewSessionManager,
    pairs: list[tuple[UUID, UUID]],
    outcome: dict[UUID, str],
) -> list[asyncio.Task]:
    tasks = []
    for project_id, interview_id in pairs:
        started = asyncio.Event()
        tasks.append(
            asyncio.create_task(
                _hold(manager, project_id, interview_id, outcome, started)
            )
        )
        await started.wait()
    return tasks


def test_end_by_project_ends_only_that_project():
    async def scenario():
        manager = InterviewSessionManager()
        a, b = uuid4(), uuid4()
        pairs = [(a, uuid4()), (a, uuid4()), (b, uuid4())]
        outcome: dict[UUID, str] = {}
        tasks = await _start(manager, pairs, outcome)

        assert await manager.end([a]) == 2

        assert outcome == {pairs[0][1]: "deleted", pairs[1][1]: "deleted"}
        assert not tasks[2].done()
        tasks[2].cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_end_by_interview_is_scoped_to_the_project():
    """An interview id from another project must not end that project's session:
    the ids come from a request body, and the delete itself is project-scoped."""

    async def scenario():
        manager = InterviewSessionManager()
        mine, theirs = uuid4(), uuid4()
        target, other_mine, foreign = uuid4(), uuid4(), uuid4()
        outcome: dict[UUID, str] = {}
        tasks = await _start(
            manager,
            [(mine, target), (mine, other_mine), (theirs, foreign)],
            outcome,
        )

        assert await manager.end([mine], [target, foreign]) == 1

        assert outcome == {target: "deleted"}
        for task in tasks[1:]:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_takeover_is_not_reported_as_a_delete():
    async def scenario():
        manager = InterviewSessionManager()
        project_id, interview_id = uuid4(), uuid4()
        outcome: dict[UUID, str] = {}
        first = await _start(manager, [(project_id, interview_id)], outcome)

        # A reconnect claims the same interview, which cancels the first.
        second = await _start(manager, [(project_id, interview_id)], {})

        assert outcome == {interview_id: "replaced"}
        second[0].cancel()
        await asyncio.gather(*first, *second, return_exceptions=True)

    asyncio.run(scenario())


def test_end_with_no_live_sessions_is_a_no_op():
    async def scenario():
        assert await InterviewSessionManager().end([uuid4()], [uuid4()]) == 0

    asyncio.run(scenario())
