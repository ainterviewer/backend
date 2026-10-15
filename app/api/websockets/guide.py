"""The guide editor's websocket: y-sync and awareness over one room per
project (`app/collab/rooms.py`).

Authenticated with the dashboard's `access_token` cookie. Editors may change
the doc; viewers sync and show presence, and their changes are dropped.
"""

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from jose import JWTError
from pydantic import UUID4, ValidationError

from ...auth import AuthToken
from ...collab.rooms import DocMismatchError, ReadOnlyChannel, guide_rooms
from ...dependencies import DBSession
from ...types import CollaboratorRole, Scope
from .auth import WS_UNAUTHORIZED

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ws", tags=["guide"])

#: Close code for "signed in, but not a collaborator on this project".
#: Private-use range, echoing HTTP 403.
WS_FORBIDDEN = 4403
#: Close code for "your copy of the doc is of another lineage": drop it and
#: connect again without `doc_id`. Echoes HTTP 409.
WS_DOC_MISMATCH = 4409


class WebSocketChannel:
    """A FastAPI websocket as a pycrdt `Channel`."""

    def __init__(self, websocket: WebSocket, path: str):
        self._websocket = websocket
        self._path = path

    @property
    def path(self) -> str:
        return self._path

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        try:
            return await self.recv()
        except WebSocketDisconnect:
            raise StopAsyncIteration from None

    async def send(self, message: bytes) -> None:
        # The room broadcasts from its own task group, where a failed send
        # would stop the room for everyone: a socket that has gone away just
        # stops receiving.
        try:
            await self._websocket.send_bytes(message)
        except Exception:
            logger.debug("Dropped a guide doc message to a closed socket")

    async def recv(self) -> bytes:
        return await self._websocket.receive_bytes()


def _role(db, token: AuthToken, project_id: UUID4) -> CollaboratorRole | None:
    scopes = {Scope(s) for s in token.scope.split()}
    if Scope.ADMIN in scopes:
        return CollaboratorRole.ADMIN
    if not any(scope.includes(Scope.DEMO) for scope in scopes):
        return None
    return db.projects.get_user_role_on_project(token.user_id, project_id)


@router.websocket("/guide/{project_id}")
async def guide_websocket(
    websocket: WebSocket,
    project_id: UUID4,
    db: DBSession,
    doc_id: str | None = None,
):
    """`doc_id`: the lineage of the copy the client holds, when reconnecting
    (the doc's `meta.doc_id`)."""
    # Accepted before refusing, so the browser can read the close code (see
    # `WS_UNAUTHORIZED`).
    await websocket.accept()
    cookie = websocket.cookies.get("access_token")
    try:
        token = AuthToken.decode(cookie) if cookie else None
    except (JWTError, ValidationError):
        token = None
    if token is None:
        await websocket.close(WS_UNAUTHORIZED, "Not signed in")
        return
    role = _role(db, token, project_id)
    # Released before the long-lived part: the room opens its own sessions.
    db.session.close()
    if role is None or not role.includes(CollaboratorRole.VIEWER):
        await websocket.close(WS_FORBIDDEN, "Not a collaborator on this project")
        return

    channel = WebSocketChannel(websocket, f"guide/{project_id}/{token.user_id}")
    try:
        await guide_rooms.serve(
            project_id,
            channel
            if role.includes(CollaboratorRole.EDITOR)
            else ReadOnlyChannel(channel),
            doc_id,
        )
    except DocMismatchError:
        await websocket.close(WS_DOC_MISMATCH, "The guide was reset; reload it")
