"""Co-editing the guide over `/ws/guide/{project_id}`, end to end.

A real server (uvicorn, in a thread) and real y-sync clients (pycrdt's
`Provider` over `websockets`), because what is pinned here is the wiring:
that two editors converge, that the room projects the doc into
`projectcontent.guide`, that a doc that does not make a guide never
overwrites it, that a viewer cannot write, and that a write to the column
the doc did not make reaches the editors who have it open.
"""

import asyncio
import contextlib
import json
import socket
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager

import pytest
import uvicorn
from pycrdt import Doc, Provider, Text
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from app.auth import AuthToken
from app.collab import rooms
from app.collab.guide_doc import doc_id, roots
from app.db import InterviewDataBase
from app.db.tables import (
    Base,
    CollaboratorTable,
    GuideDocTable,
    ProjectContentTable,
    ProjectFolderTable,
    UserTable,
)
from app.db.triggers import register_trigger_functions
from app.dependencies import get_db
from app.main import app
from app.types import CollaboratorRole, Scope

pytestmark = pytest.mark.anyio


@pytest.fixture
def server(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")

    @event.listens_for(engine, "connect")
    def _enforce(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    register_trigger_functions(engine)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        users = {
            role: UserTable(email=f"{role}@example.com", password="x", first_name=role)
            for role in ("editor", "other", "viewer", "stranger")
        }
        folder = ProjectFolderTable(title="f")
        session.add_all([*users.values(), folder])
        session.flush()
        for name, role in (
            ("editor", CollaboratorRole.EDITOR),
            ("other", CollaboratorRole.EDITOR),
            ("viewer", CollaboratorRole.VIEWER),
        ):
            session.add(
                CollaboratorTable(
                    folder_id=folder.id, user_id=users[name].id, role=role
                )
            )
        session.commit()
        project_id = InterviewDataBase(session).projects.create_project(
            folder_id=folder.id,
            title="p",
            owner_id=users["editor"].id,
            default_language="EN",
        )
        tokens = {
            name: AuthToken(user_id=user.id, scope=Scope.USER).encode()
            for name, user in users.items()
        }

    @contextmanager
    def db():
        with Session(engine) as session:
            yield InterviewDataBase(session)

    def _db():
        with db() as database:
            yield database

    app.dependency_overrides[get_db] = _db
    registry = rooms.GuideRooms(db)
    monkeypatch.setattr(rooms, "guide_rooms", registry)
    for module in ("app.api.websockets.guide", "app.api.dashboard.versions"):
        monkeypatch.setattr(f"{module}.guide_rooms", registry)
    monkeypatch.setattr(rooms, "SAVE_DELAY", 0.05)

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = uvicorn.Config(app, port=port, log_level="warning", lifespan="off")
    uv = uvicorn.Server(config)
    thread = threading.Thread(target=uv.run, daemon=True)
    thread.start()
    while not uv.started:
        time.sleep(0.01)

    yield f"ws://127.0.0.1:{port}/ws/guide/{project_id}", tokens, db, project_id

    uv.should_exit = True
    thread.join(5)
    app.dependency_overrides.clear()


class Client:
    """A websockets connection as a pycrdt `Channel`."""

    def __init__(self, websocket):
        self._websocket = websocket

    @property
    def path(self) -> str:
        return "test"

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        try:
            return await self.recv()
        except ConnectionClosed:
            raise StopAsyncIteration from None

    async def send(self, message: bytes) -> None:
        with contextlib.suppress(ConnectionClosed):
            await self._websocket.send(message)

    async def recv(self) -> bytes:
        message = await self._websocket.recv()
        assert isinstance(message, bytes)
        return message


@contextlib.asynccontextmanager
async def editor(url: str, token: str):
    doc = Doc()
    headers = {"Cookie": f"access_token={token}"}
    async with (
        connect(url, additional_headers=headers) as ws,
        Provider(doc, Client(ws)),
    ):
        await until(lambda: doc_id(doc) is not None)
        yield doc


async def until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.02)


def introduction(doc: Doc) -> Text:
    """The English introduction, created if it has none yet -- as the editor
    does on the first keystroke into an empty language."""
    _meta, top, *_ = roots(doc)
    texts = top["introduction"]
    if "EN" not in texts:
        texts["EN"] = Text()
    return texts["EN"]


def read_introduction(doc: Doc) -> str:
    """The English introduction, without creating it: polling must not race
    the text it waits for."""
    _meta, top, *_ = roots(doc)
    return str(top["introduction"].get("EN", ""))


def column(db, project_id) -> ProjectContentTable:
    with db() as database:
        content = database.session.execute(
            select(ProjectContentTable).where(
                ProjectContentTable.project_id == project_id
            )
        ).scalar_one()
        database.session.expunge(content)
        return content


def guide_doc(db, project_id) -> GuideDocTable | None:
    with db() as database:
        row = database.guide_docs.get(project_id)
        if row is not None:
            database.session.expunge(row)
        return row


async def test_two_editors_converge_and_the_guide_is_saved(server):
    url, tokens, db, project_id = server
    revision = column(db, project_id).guide_revision

    async with editor(url, tokens["editor"]) as a, editor(url, tokens["other"]) as b:
        introduction(a).insert(0, "Hello")
        await until(lambda: read_introduction(b) == "Hello")
        introduction(b).insert(len(b"Hello"), " there")
        await until(lambda: read_introduction(a) == "Hello there")

        await until(
            lambda: column(db, project_id).guide.introduction.get("EN") == "Hello there"
        )

    content = column(db, project_id)
    assert content.guide_revision > revision
    stored = guide_doc(db, project_id)
    assert stored is not None and stored.errors is None
    assert stored.guide_revision == content.guide_revision


async def test_a_viewer_cannot_change_the_guide(server):
    url, tokens, db, project_id = server

    async with editor(url, tokens["editor"]) as a, editor(url, tokens["viewer"]) as v:
        introduction(a).insert(0, "Hi")
        await until(lambda: read_introduction(v) == "Hi")
        introduction(v).insert(0, "Vandalised ")
        await asyncio.sleep(0.3)
        assert str(introduction(a)) == "Hi"

    assert column(db, project_id).guide.introduction.get("EN") == "Hi"


async def test_a_doc_that_is_not_a_guide_never_overwrites_it(server):
    url, tokens, db, project_id = server

    async with editor(url, tokens["editor"]) as a:
        introduction(a).insert(0, "Kept")
        await until(
            lambda: column(db, project_id).guide.introduction.get("EN") == "Kept"
        )
        _meta, top, *_ = roots(a)
        top["ai_generated_sections"] = json.dumps("not a number")
        await until(lambda: bool((guide_doc(db, project_id) or GuideDocTable()).errors))

    assert column(db, project_id).guide.introduction.get("EN") == "Kept"
    with db() as database:
        assert database.versions.status(project_id).guide_errors


async def test_a_write_the_doc_did_not_make_reaches_its_editors(server):
    url, tokens, db, project_id = server

    async with editor(url, tokens["editor"]) as a:
        introduction(a).insert(0, "Typed")
        await until(
            lambda: column(db, project_id).guide.introduction.get("EN") == "Typed"
        )

        # A restore, a translation: written straight to the column.
        with db() as database:
            guide = database.projects.get_guide(project_id)
            guide.framing["EN"] = "From the server"
            database.projects.update_guide(project_id, guide)

        await until(
            lambda: str(roots(a)[1]["framing"].get("EN", "")) == "From the server"
        )
        assert str(introduction(a)) == "Typed"


async def test_strangers_and_the_signed_out_are_turned_away(server):
    url, tokens, *_ = server

    for headers, code in (
        ({}, 4401),
        ({"Cookie": f"access_token={tokens['stranger']}"}, 4403),
    ):
        async with connect(url, additional_headers=headers) as ws:
            with pytest.raises(ConnectionClosed) as closed:
                await ws.recv()
        assert closed.value.rcvd is not None and closed.value.rcvd.code == code


async def test_a_copy_of_another_lineage_is_told_to_start_over(server):
    url, tokens, *_ = server
    headers = {"Cookie": f"access_token={tokens['editor']}"}

    async with connect(f"{url}?doc_id=not-this-one", additional_headers=headers) as ws:
        with pytest.raises(ConnectionClosed) as closed:
            while True:
                await ws.recv()
    assert closed.value.rcvd is not None and closed.value.rcvd.code == 4409
