"""Speech-to-text: the upstream realtime transcription session.

Owns the whole upstream protocol -- URL construction, the session.update
handshake, base64 PCM framing and the event stream -- so the websocket
endpoint only has to move bytes between the participant and this session.
"""

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from typing import Self
from uuid import UUID

import websockets
from uvicorn.config import logger
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed
from websockets.exceptions import WebSocketException as WSException

from ainterviewer.settings import settings as lib_settings
from ainterviewer.types import LanguageCode

from ...settings import app_settings
from .errors import SpeechNotConfigured, SpeechUnavailable
from .sinks import SAMPLE_RATE

DEFAULT_STT_ENDPOINT = "wss://api.openai.com"

SESSION_ACK_TIMEOUT = 10


def _transcription_url(endpoint: str | None) -> str:
    """Build the upstream realtime URL.

    The model must NOT be a query parameter for transcription sessions; it is
    set via session.update in the handshake (see TranscriptionSession._init).
    """
    base = (endpoint or DEFAULT_STT_ENDPOINT).rstrip("/")
    return f"{base}/v1/realtime?intent=transcription"


def _session_update(transcription: dict) -> dict:
    return {
        "type": "session.update",
        "session": {
            "type": "transcription",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                    "turn_detection": None,
                    "transcription": transcription,
                }
            },
        },
    }


class TranscriptionSession:
    """An open realtime transcription session.

    Every method raises `SpeechUnavailable` once the upstream is gone, so the
    caller has a single failure mode to react to instead of a mix of websockets
    exceptions. `is_open` then stays False, which is how a caller that keeps
    serving its own socket (transcription is best-effort) can skip the rest of
    the frames without having to track that itself.
    """

    def __init__(self, upstream: ClientConnection):
        self._upstream = upstream
        self._open = True

    @property
    def is_open(self) -> bool:
        """False once the upstream has closed or been closed."""
        return self._open

    @classmethod
    async def open(
        cls,
        interview_id: UUID,
        language: LanguageCode | None = None,
        prompt_context: str | None = None,
    ) -> Self:
        """Connect and configure a transcription session for one interview.

        Raises `SpeechNotConfigured` when the deployment has no STT model, and
        `SpeechUnavailable` when the upstream cannot be reached or rejects the
        session configuration.
        """
        speech_settings = app_settings.services.speech
        if (stt_model := speech_settings.stt_model) is None:
            raise SpeechNotConfigured("Transcription not configured (services.speech)")

        try:
            upstream = await websockets.connect(
                _transcription_url(speech_settings.stt_endpoint),
                additional_headers={
                    "Authorization": "Bearer "
                    + lib_settings.secrets.openai_api_key.get_secret_value(),
                    "OpenAI-Safety-Identifier": str(interview_id),
                },
                max_size=None,
            )
        except (OSError, WSException, TimeoutError) as e:
            raise SpeechUnavailable(f"Transcription upstream unreachable: {e!r}")

        session = cls(upstream)
        try:
            await session._init(stt_model, language, prompt_context)
        except (OSError, WSException, TimeoutError) as e:
            await session.close()
            raise SpeechUnavailable(f"Transcription session failed: {e!r}")
        return session

    async def _await_session_response(self) -> dict:
        """Consume upstream events until a session.update is acknowledged."""
        while True:
            event = json.loads(
                await asyncio.wait_for(
                    self._upstream.recv(), timeout=SESSION_ACK_TIMEOUT
                )
            )
            if event.get("type") in ("session.updated", "error"):
                return event

    async def _init(
        self,
        stt_model: str,
        language: LanguageCode | None,
        prompt_context: str | None,
    ) -> None:
        """Configure the upstream session: PCM16 input, no server VAD (the
        client commits the buffer manually on send), and the interview
        language. Raises WSException if this core configuration is rejected.

        `prompt_context` (the last interviewer question) is then added as a
        transcription prompt in a second, best-effort update: not all models
        support `prompt`, and a session update fails atomically, so it must not
        ride along with the core config.
        """
        transcription: dict = {
            "model": stt_model,
            "delay": app_settings.services.speech.sst_delay,
        }
        if language is not None:
            transcription["language"] = language.lower()

        await self._upstream.send(json.dumps(_session_update(transcription)))
        event = await self._await_session_response()
        if event.get("type") == "error":
            raise WSException(
                f"Transcription session config rejected: {event['error']}"
            )

        if prompt_context:
            prompted = transcription | {"prompt": "Q: " + prompt_context + "\nA:"}
            await self._upstream.send(json.dumps(_session_update(prompted)))
            event = await self._await_session_response()
            if event.get("type") == "error":
                logger.warning(
                    "Transcription prompt not applied: "
                    f"{event['error'].get('message', event['error'])}"
                )

    async def events(self) -> AsyncIterator[str | bytes]:
        """Yield upstream transcription events until the session closes.

        A closed upstream ends the iteration rather than raising: the caller is
        relaying these into a socket it has to keep serving either way.
        """
        try:
            async for message in self._upstream:
                yield message
        except ConnectionClosed:
            pass
        finally:
            self._open = False

    async def append_audio(self, pcm: bytes) -> None:
        """Append one PCM16 frame to the upstream input buffer."""
        try:
            await self._upstream.send(
                json.dumps(
                    {
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(pcm).decode(),
                    }
                )
            )
        except ConnectionClosed as e:
            self._open = False
            raise SpeechUnavailable(f"Transcription upstream closed: {e!r}")

    async def send_control(self, message: str) -> None:
        """Pass a client control message (commit, session.update, ...) through
        to the upstream unchanged."""
        try:
            await self._upstream.send(message)
        except ConnectionClosed as e:
            self._open = False
            raise SpeechUnavailable(f"Transcription upstream closed: {e!r}")

    async def close(self) -> None:
        self._open = False
        await self._upstream.close()
