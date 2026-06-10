import asyncio
import base64
import json
import time

import websockets
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, WebSocketException
from jose import JWTError
from uvicorn.config import logger
from websockets.exceptions import ConnectionClosed
from websockets.exceptions import WebSocketException as WSException

from ainterviewer.settings import settings as lib_settings

from ....auth import InterviewToken
from ....services.audio import LocalWavSink
from ....settings import app_settings

router = APIRouter(prefix="/ws", tags=["interviews"])

DEFAULT_STT_ENDPOINT = "wss://api.openai.com"


def _transcription_url() -> str | None:
    """Build the upstream realtime URL, or None if STT is not configured.

    The model must NOT be a query parameter for transcription sessions; the
    client sets it via session.update (we hand it over in the ready message).
    """
    speech = app_settings.services.speech
    if speech is None or speech.stt_model is None:
        return None
    endpoint = (speech.stt_endpoint or DEFAULT_STT_ENDPOINT).rstrip("/")
    return f"{endpoint}/v1/realtime?intent=transcription"


async def _notify(websocket: WebSocket, error: str) -> None:
    try:
        await websocket.send_json({"type": "error", "error": error})
    except Exception:
        pass


async def _relay_transcripts(websocket: WebSocket, upstream) -> None:
    """Forward OpenAI transcription events from upstream back to the client."""
    try:
        async for message in upstream:
            if isinstance(message, bytes):
                await websocket.send_bytes(message)
            else:
                await websocket.send_text(message)
    except ConnectionClosed:
        pass


@router.websocket("/transcribe")
async def transcription_websocket_endpoint(websocket: WebSocket):
    """Tee participant audio: persist the recording (source of truth) while
    forwarding it to the OpenAI-compatible transcription service best-effort.

    Client sends raw PCM16 binary frames plus JSON control messages (which own
    the upstream session handshake). Transcripts are relayed straight back; the
    client submits the final text through the normal interview message path.
    """
    token = websocket.cookies.get("interview_token")
    if token is None:
        raise WebSocketException(401, "Unauthorized")
    try:
        interview_token = InterviewToken.decode(token)
    except JWTError as e:
        raise WebSocketException(401, str(e))

    await websocket.accept()

    audio_dir = lib_settings.storage.interview_storage.audio_path(
        interview_token.interview_id
    )
    sink = LocalWavSink(audio_dir / f"recording-{int(time.time())}.wav")

    upstream = None
    relay_task = None
    transcription_up = False

    speech = app_settings.services.speech
    stt_model = speech.stt_model if speech is not None else None
    transcription_url = _transcription_url()
    headers = {
        "Authorization": "Bearer "
        + lib_settings.secrets.openai_api_key.get_secret_value(),
        "OpenAI-Safety-Identifier": str(interview_token.interview_id),
    }

    try:
        if transcription_url is None:
            logger.error("Transcription not configured (services.speech)")
            await _notify(websocket, "transcription_unavailable")
        else:
            try:
                upstream = await websockets.connect(
                    transcription_url, additional_headers=headers, max_size=None
                )
                transcription_up = True
                relay_task = asyncio.create_task(
                    _relay_transcripts(websocket, upstream)
                )
                # The client owns the session handshake but only the server
                # knows the configured model — hand it over.
                await websocket.send_json({"type": "ready", "model": stt_model})
            except (OSError, WSException) as e:
                logger.error(f"Transcription upstream unavailable: {e!r}")
                await _notify(websocket, "transcription_unavailable")

        while True:
            msg = await websocket.receive()
            if msg["type"] == "websocket.disconnect":
                break

            if (pcm := msg.get("bytes")) is not None:
                sink.write(pcm)  # unconditional: recording is the source of truth
                if transcription_up and upstream is not None:
                    try:
                        await upstream.send(
                            json.dumps(
                                {
                                    "type": "input_audio_buffer.append",
                                    "audio": base64.b64encode(pcm).decode(),
                                }
                            )
                        )
                    except ConnectionClosed:
                        transcription_up = False
                        await _notify(websocket, "transcription_unavailable")
            elif (
                (text := msg.get("text")) is not None
                and transcription_up
                and upstream is not None
            ):
                # Control passthrough (session.update, commit, ...).
                try:
                    await upstream.send(text)
                except ConnectionClosed:
                    transcription_up = False
                    await _notify(websocket, "transcription_unavailable")

    except WebSocketDisconnect:
        pass

    finally:
        if relay_task is not None:
            relay_task.cancel()
        if upstream is not None:
            await upstream.close()
        sink.close()
