"""Rooms: the docs editors of a setup page are connected to, one per
project and live part (guide, consent, welcome; `parts.py`).

A room holds the part's doc, relays the y-sync and awareness protocols
between its editors (`pycrdt.websocket.YRoom`), and saves: a couple of
seconds after the last change, the doc's state is stored and, when it makes
a valid value, projected into the part's column on `projectcontent`, which
moves the part's revision. Everything else -- publishing, tests, analysis
-- goes on reading the column.

**The registry is per-process**, like `InterviewSessionManager`. Two workers
would hold two rooms for one project, each saving over the other; more than
one worker needs the rooms relayed (Redis pub/sub) first.

Writes to the column the doc did not make -- restore, translation, a
language removed, the PUTs -- are merged into an open room's doc rather
than lost under its next save: a session listener (below) notices the
column changing on commit and asks the room to save, and a save that finds
the column moved first merges that write in (`merge_guide_data`).
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
from sqlalchemy import event, inspect
from sqlalchemy.orm import Session, object_session

from ..db import InterviewDataBase
from ..db.tables import ProjectContentTable
from ..db.types import ContentPart, ContentVersionKind
from .guide_doc import doc_id, merge_guide_data
from .parts import LIVE_PARTS, DocPart

logger = logging.getLogger(__name__)

#: How long after the last change a room saves.
SAVE_DELAY = 2.0
#: How long a room goes without changes before keeping an `auto` version of
#: what it saved.
SNAPSHOT_IDLE = 300.0
#: The transaction origin of the server's own writes to a doc.
SERVER_ORIGIN = "server"

type DBFactory = Callable[[], AbstractContextManager[InterviewDataBase]]


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


class ContentRoom:
    def __init__(
        self,
        project_id: UUID,
        part: DocPart,
        db: DBFactory,
        doc: Doc,
        synced_revision: int,
        base: dict[str, Any],
        languages: tuple[str, ...],
        default_language: str,
        errors: list[str] | None,
    ):
        self.project_id = project_id
        self.part = part
        self._db = db
        self.doc = doc
        self.doc_id = doc_id(doc)
        #: The part's revision the doc last agreed with, and the part's JSON
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
        #: A save changed the part since the last `auto` version.
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
                logger.exception(
                    "Saving the %s doc of %s failed", self.part.part, self.project_id
                )

    async def _snapshotter(self) -> None:
        while True:
            await asyncio.sleep(SNAPSHOT_IDLE / 5)
            idle = time.monotonic() - self._last_change
            if self._unsnapshotted and idle >= SNAPSHOT_IDLE:
                try:
                    await self._snapshot()
                except Exception:
                    logger.exception(
                        "Snapshotting the content of %s failed", self.project_id
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
        errors that keep it from being valid, if any."""
        part = self.part
        async with self._lock:
            for _ in range(5):
                data = part.read(self.doc)
                value, errors = part.validate(data, self.default_language)
                state = self.doc.get_update()

                def write(value=value, errors=errors, state=state):
                    with self._db() as db:
                        revision = db.content_docs.save(
                            self.project_id,
                            part.part,
                            state,
                            self.synced_revision,
                            value,
                            errors or None,
                        )
                        # This write is the doc's own: no need to merge it
                        # back in.
                        db.session.info.get(_CHANGED, set()).discard(
                            (self.project_id, part.part)
                        )
                        db.session.commit()
                        column = db.content_docs.column(self.project_id, part.part)
                        return revision, column.languages, column.default_language

                revision, languages, self.default_language = await asyncio.to_thread(
                    write
                )
                if languages != self.languages:
                    # A language added or removed: every text gets one in it,
                    # or loses it (its text went with the language already).
                    self.languages = languages
                    part.apply(self.doc, part.read(self.doc), languages, SERVER_ORIGIN)
                if revision is not None:
                    if revision != self.synced_revision:
                        self._unsnapshotted = True
                    self.synced_revision = revision
                    if not errors:
                        self.base = part.dump(value)
                    self.errors = errors or None
                    self._announce()
                    return self.errors
                await self._merge_column()
            raise RuntimeError(f"The {part.part} kept changing under the doc")

    async def _merge_column(self) -> None:
        """Merge a write to the column the doc did not make into the doc."""

        part = self.part

        def read():
            with self._db() as db:
                return db.content_docs.column(self.project_id, part.part)

        column = await asyncio.to_thread(read)
        theirs = part.dump(column.value)
        merged = merge_guide_data(self.base, theirs, part.read(self.doc))
        self.languages = column.languages
        part.apply(self.doc, merged, column.languages, SERVER_ORIGIN)
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
                # been saved even when the part's revision did not move.
                "saved_at": time.time(),
            }
        )


type RoomKey = tuple[UUID, ContentPart]


class ContentRooms:
    """The registry. See the module docstring."""

    def __init__(self, db: DBFactory):
        self._db = db
        self._rooms: dict[RoomKey, ContentRoom] = {}
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def get(self, project_id: UUID, part: ContentPart) -> ContentRoom | None:
        return self._rooms.get((project_id, part))

    async def _open(self, project_id: UUID, part: DocPart) -> ContentRoom:
        def load():
            with self._db() as db:
                column = db.content_docs.column(project_id, part.part)
                row = db.content_docs.get(project_id, part.part)
                if row is None:
                    return column, None, None, None
                return column, row.state, row.revision, row.errors

        column, state, doc_revision, errors = await asyncio.to_thread(load)
        value, revision = part.dump(column.value), column.revision
        doc = Doc()
        if state is None:
            part.apply(doc, value, column.languages, SERVER_ORIGIN)
            base, synced = value, revision
        else:
            doc.apply_update(state)
            # Languages may have come or gone while it was closed.
            part.apply(doc, part.read(doc), column.languages, SERVER_ORIGIN)
            # When the doc last agreed with the column. If the column has
            # moved since, the first save merges that in, from here.
            base = value if doc_revision == revision else part.read(doc)
            synced = doc_revision
        room = ContentRoom(
            project_id,
            part,
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
        self,
        project_id: UUID,
        part: ContentPart,
        channel: Channel,
        doc_id: str | None = None,
    ) -> None:
        """Serve one editor's connection until it closes; the room closes,
        saving, with its last editor. `doc_id` is the lineage of the copy
        the client already holds, if any; DocMismatchError if it is not the
        room's."""
        key = (project_id, part)
        async with self._lock:
            self._loop = asyncio.get_running_loop()
            room = self._rooms.get(key)
            if room is None:
                room = self._rooms[key] = await self._open(project_id, LIVE_PARTS[part])
        try:
            if doc_id is not None and doc_id != room.doc_id:
                raise DocMismatchError(doc_id)
            await room.serve(channel)
        finally:
            async with self._lock:
                if room.clients == 0 and self._rooms.get(key) is room:
                    del self._rooms[key]
                    await room.stop()
                    del room
                    # pycrdt's objects must be freed on the thread that made
                    # them, and a closed room leaves cycles (the YRoom and its
                    # awareness callbacks). Left to the collector, they can
                    # be freed on a worker thread, where pycrdt refuses and
                    # leaks them. Rooms close rarely; collect here.
                    gc.collect()

    async def flush(self, project_id: UUID) -> None:
        """Save a project's open rooms now: before publishing, so what is
        published is what editors see."""
        for key, room in list(self._rooms.items()):
            if key[0] == project_id:
                await room.save()

    def column_changed(self, keys: set[RoomKey]) -> None:
        """Something committed a change to these parts' columns. Thread-safe."""
        if self._loop is None or not keys & self._rooms.keys():
            return
        self._loop.call_soon_threadsafe(self._schedule_saves, keys)

    def _schedule_saves(self, keys: set[RoomKey]) -> None:
        for key in keys:
            if (room := self._rooms.get(key)) is not None:
                task = asyncio.create_task(room.save())
                task.add_done_callback(_log_failure)


def _log_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and (error := task.exception()) is not None:
        logger.error("Merging a write into its doc failed", exc_info=error)


def _default_db() -> AbstractContextManager[InterviewDataBase]:
    from contextlib import contextmanager

    from ..dependencies import engine

    @contextmanager
    def session():
        with Session(engine) as s:
            yield InterviewDataBase(s)

    return session()


content_rooms = ContentRooms(_default_db)


# ---- noticing writes to the live parts' columns ----

#: `session.info` key: the (project, part) rooms a session's commit concerns.
_CHANGED = "content_doc_changed"
#: Fields that concern every live part: an open doc gives every text one per
#: language.
_LANGUAGE_FIELDS = ("languages", "default_language")


@event.listens_for(ProjectContentTable, "before_update")
def _note_change(_mapper, _connection, target: ProjectContentTable) -> None:
    attrs = inspect(target).attrs
    languages = any(attrs[f].history.has_changes() for f in _LANGUAGE_FIELDS)
    parts = {
        part
        for part in LIVE_PARTS
        if languages or attrs[part.value].history.has_changes()
    }
    if parts and (session := object_session(target)) is not None:
        changed = session.info.setdefault(_CHANGED, set())
        changed.update((target.project_id, part) for part in parts)


@event.listens_for(Session, "after_commit")
def _notify_rooms(session: Session) -> None:
    if keys := session.info.pop(_CHANGED, None):
        content_rooms.column_changed(keys)


@event.listens_for(Session, "after_rollback")
def _forget(session: Session) -> None:
    session.info.pop(_CHANGED, None)
