"""Rooms: the guide docs editors are connected to, one per project.

A room holds the project's doc (`guide_doc`), relays the y-sync and
awareness protocols between its editors (`pycrdt.websocket.YRoom`), and
saves: a couple of seconds after the last change, the doc's state is stored
and, when it makes a valid guide, projected into `projectcontent.guide`,
which moves the guide's revision. Everything else -- publishing, tests,
analysis -- goes on reading the column.

**The registry is per-process**, like `InterviewSessionManager`. Two workers
would hold two rooms for one project, each saving over the other; more than
one worker needs the rooms relayed (Redis pub/sub) first.

Writes to the column the doc did not make -- restore, translation, a
language removed, the import PUT -- are merged into an open room's doc
rather than lost under its next save: a session listener (below) notices
the guide changing on commit and asks the room to save, and a save that
finds the column moved first merges that write in (`merge_guide_data`).
With no room open the next one to open does the same.

The server's own state reaches editors through awareness, as the server's
entry: `{"server": true, "doc_id": ..., "revision": n, "errors": [...],
"saved_at": t}`.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any
from uuid import UUID

from pycrdt import (
    Channel,
    Doc,
    YMessageType,
    YSyncMessageType,
    create_awareness_message,
)
from pycrdt.websocket import YRoom
from pydantic import ValidationError
from sqlalchemy import event, inspect
from sqlalchemy.orm import Session, object_session

from ainterviewer.interview_guides.localized import LocalizedInterviewGuide

from ..db import InterviewDataBase
from ..db.tables import ProjectContentTable
from ..db.types import ContentVersionKind
from .guide_doc import (
    apply_guide,
    apply_guide_data,
    doc_id,
    merge_guide_data,
    read_guide_data,
)

logger = logging.getLogger(__name__)

#: How long after the last change a room saves.
SAVE_DELAY = 2.0
#: How long a room goes without changes before keeping an `auto` version of
#: what it saved.
SNAPSHOT_IDLE = 300.0
#: The transaction origin of the server's own writes to a doc.
SERVER_ORIGIN = "server"

type DBFactory = Callable[[], AbstractContextManager[InterviewDataBase]]


def validate(
    data: dict[str, Any], language: str
) -> tuple[LocalizedInterviewGuide | None, list[str]]:
    """The guide `data` makes, or why it makes none. Held to what the PUT
    holds a guide to, conditions included."""
    try:
        guide = LocalizedInterviewGuide.model_validate(data)
    except ValidationError as error:
        return None, [
            f"{'.'.join(str(part) for part in e['loc'])}: {e['msg']}"
            for e in error.errors()
        ]
    if order_errors := guide.render(language).condition_order_errors():
        return None, [
            "Conditions must read questions asked before the one they gate: "
            + "; ".join(order_errors)
        ]
    _trim_options(guide)
    return guide, []


def _trim_options(guide: LocalizedInterviewGuide) -> None:
    """Strip stray whitespace from survey options' text, in the guide saved
    rather than in the doc: answers are matched against option text verbatim,
    and trimming the doc would eat the space someone just typed."""
    for section in guide.question_sections:
        for question in section.questions:
            for option in getattr(question.survey_item, "options", None) or []:
                option.text = {lang: text.strip() for lang, text in option.text.items()}


class ReadOnlyChannel(Channel):
    """A viewer's connection: it syncs and shows presence, and whatever it
    sends to change the doc is dropped."""

    def __init__(self, channel: Channel):
        self._channel = channel

    @property
    def path(self) -> str:
        return self._channel.path

    async def send(self, message: bytes) -> None:
        await self._channel.send(message)

    async def recv(self) -> bytes:
        return await self.__anext__()

    async def __anext__(self) -> bytes:
        while True:
            message = await self._channel.__anext__()
            if (
                len(message) > 1
                and message[0] == YMessageType.SYNC
                and message[1] != YSyncMessageType.SYNC_STEP1
            ):
                continue
            return message


class DocMismatchError(Exception):
    """A client's copy of the doc is of another lineage than the room's: the
    doc was dropped and seeded again since it loaded. Merging it would add
    every item a second time, so it must start over."""


class GuideRoom:
    def __init__(
        self,
        project_id: UUID,
        db: DBFactory,
        doc: Doc,
        synced_revision: int,
        base: dict[str, Any],
        languages: tuple[str, ...],
        default_language: str,
        errors: list[str] | None,
    ):
        self.project_id = project_id
        self._db = db
        self.doc = doc
        self.doc_id = doc_id(doc)
        #: The guide revision the doc last agreed with, and the guide's JSON
        #: then: the base of a merge when the column moves.
        self.synced_revision = synced_revision
        self.base = base
        #: The project's languages: every text has one per language.
        self.languages = languages
        self.default_language = default_language
        self.errors = errors
        self.room = YRoom(ydoc=doc, log=logger)
        self._lock = asyncio.Lock()
        self._changed = asyncio.Event()
        #: A save changed the guide since the last `auto` version.
        self._unsnapshotted = False
        self._last_change = time.monotonic()
        self._tasks: list[asyncio.Task] = []
        self._subscription = None

    # ---- lifecycle ----

    async def start(self) -> None:
        self._tasks.append(asyncio.create_task(self.room.start()))
        await self.room.started.wait()
        self._subscription = self.doc.observe(self._on_change)
        self._tasks.append(asyncio.create_task(self._saver()))
        self._tasks.append(asyncio.create_task(self._snapshotter()))
        self._announce()

    async def stop(self) -> None:
        if self._subscription is not None:
            self.doc.unobserve(self._subscription)
        for task in self._tasks[1:]:
            task.cancel()
        await self.save()
        if self._unsnapshotted:
            await self._snapshot()
        await self.room.stop()
        self._tasks[0].cancel()

    async def serve(self, channel: Channel) -> None:
        # Everyone already here, and the server's own state: y-websocket
        # clients never ask, and would otherwise see the others only when
        # they next renew their presence (every 15 s).
        states = list(self.room.awareness.states)
        if states:
            await channel.send(
                create_awareness_message(
                    self.room.awareness.encode_awareness_update(states)
                )
            )
        await self.room.serve(channel)

    @property
    def clients(self) -> int:
        return len(self.room.clients)

    # ---- saving ----

    def _on_change(self, _event) -> None:
        self._last_change = time.monotonic()
        self._changed.set()

    async def _saver(self) -> None:
        while True:
            await self._changed.wait()
            await asyncio.sleep(SAVE_DELAY)
            self._changed.clear()
            try:
                await self.save()
            except Exception:
                logger.exception("Saving the guide doc of %s failed", self.project_id)

    async def _snapshotter(self) -> None:
        while True:
            await asyncio.sleep(SNAPSHOT_IDLE / 5)
            idle = time.monotonic() - self._last_change
            if self._unsnapshotted and idle >= SNAPSHOT_IDLE:
                try:
                    await self._snapshot()
                except Exception:
                    logger.exception(
                        "Snapshotting the guide of %s failed", self.project_id
                    )

    async def _snapshot(self) -> None:
        def write():
            with self._db() as db:
                db.versions.snapshot(self.project_id, ContentVersionKind.AUTO)
                db.session.commit()

        await asyncio.to_thread(write)
        self._unsnapshotted = False

    async def save(self) -> list[str] | None:
        """Save the doc now, merging in whatever wrote the column since. The
        errors that keep it from being a guide, if any."""
        async with self._lock:
            for _ in range(5):
                data = read_guide_data(self.doc)
                guide, errors = validate(data, self.default_language)
                state = self.doc.get_update()

                def write(guide=guide, errors=errors, state=state):
                    with self._db() as db:
                        revision = db.guide_docs.save(
                            self.project_id,
                            state,
                            self.synced_revision,
                            guide,
                            errors or None,
                        )
                        # This write is the doc's own: no need to merge it
                        # back in.
                        db.session.info.get("guide_changed", set()).discard(
                            self.project_id
                        )
                        db.session.commit()
                        column = db.guide_docs.column(self.project_id)
                        return revision, column.languages, column.default_language

                revision, languages, self.default_language = await asyncio.to_thread(
                    write
                )
                if languages != self.languages:
                    # A language added or removed: every text gets one in it,
                    # or loses it (its text went with the language already).
                    self.languages = languages
                    apply_guide_data(
                        self.doc, read_guide_data(self.doc), languages, SERVER_ORIGIN
                    )
                if revision is not None:
                    if revision != self.synced_revision:
                        self._unsnapshotted = True
                    self.synced_revision = revision
                    if guide is not None:
                        self.base = guide.model_dump(mode="json")
                    self.errors = errors or None
                    self._announce()
                    return self.errors
                await self._merge_column()
            raise RuntimeError("The guide kept changing under the doc")

    async def _merge_column(self) -> None:
        """Merge a write to the column the doc did not make into the doc."""

        def read():
            with self._db() as db:
                return db.guide_docs.column(self.project_id)

        column = await asyncio.to_thread(read)
        theirs = column.guide.model_dump(mode="json")
        merged = merge_guide_data(self.base, theirs, read_guide_data(self.doc))
        self.languages = column.languages
        apply_guide_data(self.doc, merged, column.languages, SERVER_ORIGIN)
        self.synced_revision = column.revision
        self.base = theirs

    def _announce(self) -> None:
        self.room.awareness.set_local_state(
            {
                "server": True,
                "doc_id": self.doc_id,
                "revision": self.synced_revision,
                "errors": self.errors or [],
                # Moves on every save, so editors can tell their changes have
                # been saved even when the guide's revision did not move.
                "saved_at": time.time(),
            }
        )


class GuideRooms:
    """The registry. See the module docstring."""

    def __init__(self, db: DBFactory):
        self._db = db
        self._rooms: dict[UUID, GuideRoom] = {}
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def get(self, project_id: UUID) -> GuideRoom | None:
        return self._rooms.get(project_id)

    async def _open(self, project_id: UUID) -> GuideRoom:
        def load():
            with self._db() as db:
                column = db.guide_docs.column(project_id)
                row = db.guide_docs.get(project_id)
                if row is None:
                    return column, None, None, None
                return column, row.state, row.guide_revision, row.errors

        column, state, doc_revision, errors = await asyncio.to_thread(load)
        guide, revision = column.guide, column.revision
        doc = Doc()
        if state is None:
            apply_guide(doc, guide, column.languages, SERVER_ORIGIN)
            base, synced = guide.model_dump(mode="json"), revision
        else:
            doc.apply_update(state)
            # Languages may have come or gone while it was closed.
            apply_guide_data(doc, read_guide_data(doc), column.languages, SERVER_ORIGIN)
            # When the doc last agreed with the column. If the column has
            # moved since, the first save merges that in, from here.
            base = (
                guide.model_dump(mode="json")
                if doc_revision == revision
                else read_guide_data(doc)
            )
            synced = doc_revision
        room = GuideRoom(
            project_id,
            self._db,
            doc,
            synced,
            base,
            column.languages,
            column.default_language,
            errors,
        )
        await room.start()
        if state is None or synced != revision:
            await room.save()
        return room

    async def serve(
        self, project_id: UUID, channel: Channel, doc_id: str | None = None
    ) -> None:
        """Serve one editor's connection until it closes; the room closes,
        saving, with its last editor. `doc_id` is the lineage of the copy
        the client already holds, if any; DocMismatchError if it is not the
        room's."""
        async with self._lock:
            self._loop = asyncio.get_running_loop()
            room = self._rooms.get(project_id)
            if room is None:
                room = self._rooms[project_id] = await self._open(project_id)
        try:
            if doc_id is not None and doc_id != room.doc_id:
                raise DocMismatchError(doc_id)
            await room.serve(channel)
        finally:
            async with self._lock:
                if room.clients == 0 and self._rooms.get(project_id) is room:
                    del self._rooms[project_id]
                    await room.stop()
                    del room
                    # pycrdt's objects must be freed on the thread that made
                    # them, and a closed room leaves cycles (the YRoom and its
                    # awareness callbacks). Left to the collector, they can
                    # be freed on a worker thread, where pycrdt refuses and
                    # leaks them. Rooms close rarely; collect here.
                    gc.collect()

    async def flush(self, project_id: UUID) -> None:
        """Save an open room now: before publishing, so what is published is
        what editors see."""
        if (room := self._rooms.get(project_id)) is not None:
            await room.save()

    def column_changed(self, project_id: UUID) -> None:
        """Something committed a change to the guide column. Thread-safe."""
        if self._loop is None or project_id not in self._rooms:
            return
        self._loop.call_soon_threadsafe(self._schedule_save, project_id)

    def _schedule_save(self, project_id: UUID) -> None:
        if (room := self._rooms.get(project_id)) is not None:
            task = asyncio.create_task(room.save())
            task.add_done_callback(_log_failure)


def _log_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and (error := task.exception()) is not None:
        logger.error("Merging a guide write into its doc failed", exc_info=error)


def _default_db() -> AbstractContextManager[InterviewDataBase]:
    from contextlib import contextmanager

    from ..dependencies import engine

    @contextmanager
    def session():
        with Session(engine) as s:
            yield InterviewDataBase(s)

    return session()


guide_rooms = GuideRooms(_default_db)


# ---- noticing writes to the guide column ----


@event.listens_for(ProjectContentTable, "before_update")
def _note_guide_change(_mapper, _connection, target: ProjectContentTable) -> None:
    # The languages too: an open doc gives every text one per language.
    attrs = inspect(target).attrs
    if any(
        attrs[field].history.has_changes()
        for field in ("guide", "languages", "default_language")
    ):
        session = object_session(target)
        if session is not None:
            session.info.setdefault("guide_changed", set()).add(target.project_id)


@event.listens_for(Session, "after_commit")
def _notify_rooms(session: Session) -> None:
    for project_id in session.info.pop("guide_changed", ()):
        guide_rooms.column_changed(project_id)


@event.listens_for(Session, "after_rollback")
def _forget(session: Session) -> None:
    session.info.pop("guide_changed", None)
