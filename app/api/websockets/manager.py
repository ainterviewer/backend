import asyncio
import logging
from collections.abc import Collection
from dataclasses import dataclass
from uuid import UUID

logger = logging.getLogger(__name__)


@dataclass
class _Session:
    task: asyncio.Task
    done: asyncio.Event
    # Set by `end` before it cancels the task, so the session can tell the
    # client its interview is gone rather than treating the cancellation as a
    # takeover by a newer connection.
    deleted: bool = False


class InterviewSessionManager:
    """Tracks one active WebSocket session per (project_id, interview_id).

    When a new connection is claimed for an interview that already has a
    live session, the previous session's task is cancelled and we wait for
    it to finish teardown before the new session proceeds. This prevents
    two AInterviewer instances from racing on writes to the same interview
    (e.g. duplicate `message_id` inserts on a probe-in-flight reconnect).

    It is also how a delete reaches the sessions it would otherwise orphan:
    every endpoint that deletes interviews calls `end` first. Without that the
    interview loop keeps running on its in-memory history and writes messages
    and tasks for an interview that no longer exists -- which is how a test
    setup deleted mid-run left 1,097 orphaned rows in production.

    The registry is in-process. Production runs a single worker (`fastapi run`
    in entrypoint.sh), so it sees every session. With several workers -- not
    yet decided -- two things degrade, deliberately accepted for now:

    * A delete handled by one worker does not reach sessions on another. Those
      end at their next write instead: the foreign key rejects it,
      `insert_message`/`insert_task` raise `InterviewGoneError`, and the
      websocket sends the restart frame. What is lost is the one reply the
      session had already generated. That backstop needs foreign keys
      enforced (the default); with them switched off, those writes orphan.
    * `claim`'s takeover only cancels a prior session on the same worker, so a
      reconnect landing on another one runs alongside the old session and
      both write to the interview.

    Fixing either properly needs state shared between workers (a broker such
    as Redis, or polling the database), which belongs with deciding how
    workers are run.
    """

    _CANCEL_TIMEOUT_S = 10.0

    def __init__(self) -> None:
        self._sessions: dict[tuple[UUID, UUID], _Session] = {}
        self._lock = asyncio.Lock()

    async def claim(self, project_id: UUID, interview_id: UUID) -> asyncio.Event:
        """Register the current task as the active session for this interview.

        If another session is already registered, cancel it and wait for
        its `done` event before returning. The caller MUST pass the
        returned event to `release()` in a `finally` block.
        """
        key = (project_id, interview_id)
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("claim() must be called from within a task")

        async with self._lock:
            existing = self._sessions.get(key)

        if existing is not None:
            logger.warning(
                "Cancelling prior websocket session for project=%s interview=%s",
                project_id,
                interview_id,
            )
            existing.task.cancel()
            try:
                await asyncio.wait_for(
                    existing.done.wait(), timeout=self._CANCEL_TIMEOUT_S
                )
            except TimeoutError:
                logger.error(
                    "Prior session for project=%s interview=%s did not exit "
                    "within %.1fs; proceeding anyway",
                    project_id,
                    interview_id,
                    self._CANCEL_TIMEOUT_S,
                )

        done = asyncio.Event()
        async with self._lock:
            self._sessions[key] = _Session(task=current, done=done)
        return done

    async def end(
        self,
        project_ids: Collection[UUID],
        interview_ids: Collection[UUID] | None = None,
    ) -> int:
        """End the live sessions of interviews about to be deleted.

        Matches the sessions in `project_ids`, narrowed to `interview_ids` when
        given; marks each as deleted, cancels it and waits for its teardown, so
        that by the time this returns nothing is left to write to the rows the
        caller is about to delete. Returns how many were ended.

        Always scoped by project, never by interview id alone: interview ids
        reach the delete endpoints in request bodies, and the deletes themselves
        are project-scoped, so an unscoped match would let a caller end live
        sessions in a project they cannot touch.
        """
        projects = set(project_ids)
        interviews = None if interview_ids is None else set(interview_ids)

        async with self._lock:
            ending = [
                session
                for (project_id, interview_id), session in self._sessions.items()
                if project_id in projects
                and (interviews is None or interview_id in interviews)
            ]

        for session in ending:
            session.deleted = True
            session.task.cancel()

        for session in ending:
            try:
                await asyncio.wait_for(
                    session.done.wait(), timeout=self._CANCEL_TIMEOUT_S
                )
            except TimeoutError:
                logger.error(
                    "A session ended for deletion did not exit within %.1fs",
                    self._CANCEL_TIMEOUT_S,
                )

        if ending:
            logger.info("Ended %d live interview session(s) for deletion", len(ending))
        return len(ending)

    def ended_by_delete(
        self, project_id: UUID, interview_id: UUID, done: asyncio.Event
    ) -> bool:
        """Whether this session was cancelled by `end` rather than replaced."""
        session = self._sessions.get((project_id, interview_id))
        return session is not None and session.done is done and session.deleted

    def release(
        self, project_id: UUID, interview_id: UUID, done: asyncio.Event
    ) -> None:
        """Remove this session from the registry and signal completion.

        Only removes the entry if it still points at this session (a newer
        connection may have already replaced it).
        """
        key = (project_id, interview_id)
        existing = self._sessions.get(key)
        if existing is not None and existing.done is done:
            del self._sessions[key]
        done.set()


session_manager = InterviewSessionManager()

# from collections import defaultdict
#
# from fastapi import WebSocket
#
# from ainterviewer.settings import settings as lib_settings
# from ainterviewer.types import MessageRole
#
# from ...types import WebSocketConversation, WebSocketUsers
#
#
# def create_websocket_interview() -> WebSocketConversation:
#     return {"message_count": 0, "users": WebSocketUsers()}
#
#
# class WebSocketConnectionManager:
#     def __init__(self):
#         self.active_connections: dict[int, dict[int, WebSocketConversation]] = (
#             defaultdict(lambda: defaultdict(create_websocket_interview))
#         )
#
#     async def connect(
#         self,
#         websocket: WebSocket,
#         project_id: int,
#         interview_id: int,
#         role: MessageRole = MessageRole.USER,
#     ):
#         await websocket.accept()
#         await websocket.send_json({"type": "data", "interview_id": interview_id})
#         interview = self.active_connections[project_id][interview_id]
#         interview["users"][role.value] = websocket
#
#         if lib_settings.debug:
#             print(f"Connected to {project_id} {interview_id} {role}")
#
#     def get_active_interview(
#         self, project_id: int, interview_id: int
#     ) -> WebSocketConversation:
#         if project_id in self.active_connections:
#             if interview_id in self.active_connections[project_id]:
#                 return self.active_connections[project_id][interview_id]
#             else:
#                 raise ValueError(
#                     f"No active interviews for interview {interview_id} in interview {project_id}"
#                 )
#         else:
#             raise ValueError(f"No active interviews for interview {project_id}")
#
#     def remove_interview(self, project_id: int, interview_id: int):
#         del self.active_connections[project_id][interview_id]
#         if len(self.active_connections[project_id]) == 0:
#             del self.active_connections[project_id]
#
#     async def disconnect(
#         self,
#         project_id: int,
#         interview_id: int,
#         role: MessageRole,
#     ):
#         # NOTE: Should the interview be deleted from this manager
#         # when the final user leaves the chat?
#         interview = self.get_active_interview(project_id, interview_id)
#         del interview["users"][role]
#         if len(interview["users"]) == 0:
#             self.remove_interview(project_id, interview_id)
#         else:
#             for connection in interview["users"].values():
#                 payload = {"type": "system", "content": f"{role} has left the chat."}
#                 await connection.send_json(payload)
#
#     async def send_personal_message(
#         self,
#         payload: dict,
#         project_id: int,
#         interview_id: int,
#         role: MessageRole,
#     ):
#         interview = self.get_active_interview(project_id, interview_id)
#         if role not in interview["users"]:
#             raise ValueError(
#                 f"No active connections for {role} in interview {interview_id} in interview {project_id}"
#             )
#         await interview["users"][role].send_json(payload)
#
#     async def broadcast_message(self, payload: dict):
#         connections = [
#             connection
#             for interview in self.active_connections.values()
#             for connection, role in interview.items()
#             if role == "user"
#         ]
#         for connection in connections:
#             await connection.send_json(payload)
