"""Websocket authentication shared by the interview endpoints.

Both endpoints authenticate with the httponly ``interview_token`` cookie the
browser attaches to the handshake.
"""

from fastapi import WebSocket
from jose import JWTError
from pydantic import ValidationError
from uvicorn.config import logger

from ...auth import InterviewToken

# Application close code for "the interview credential is missing or invalid".
# Private-use range (4000-4999), chosen to echo HTTP 401.
#
# Rejecting the handshake instead -- by raising WebSocketException before
# accept() -- makes Starlette answer with a bare HTTP 403, and a rejected
# handshake reaches the browser as an opaque error event with no status and no
# reason. The client then cannot tell a dead credential from a flaky network,
# so it retries on a backoff it can never escape. Accepting first costs one
# frame and lets the client read this code in onclose and recover.
WS_UNAUTHORIZED = 4401


async def authenticate_or_close(websocket: WebSocket) -> InterviewToken | None:
    """Accept the socket and return its interview token.

    Returns ``None`` after closing the socket with :data:`WS_UNAUTHORIZED` if
    the cookie is missing or does not decode; callers must return immediately
    in that case.
    """
    token = websocket.cookies.get("interview_token")

    await websocket.accept()

    if token is None:
        await websocket.close(WS_UNAUTHORIZED, "No interview token")
        return None

    try:
        return InterviewToken.decode(token)
    except (JWTError, ValidationError) as e:
        logger.info("Rejecting websocket with an undecodable interview token: %s", e)
        await websocket.close(WS_UNAUTHORIZED, "Invalid interview token")
        return None
