import asyncio
import base64
import json
import time

import websockets
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, WebSocketException
from jose import JWTError
from sqlalchemy.exc import NoResultFound
from uvicorn.config import logger
from websockets.exceptions import ConnectionClosed
from websockets.exceptions import WebSocketException as WSException

from ainterviewer.settings import settings as lib_settings
from ainterviewer.types import LanguageCode, MessageRole

from ....auth import InterviewToken
from ....dependencies import DBSession
from ....services.audio import SAMPLE_RATE, LocalWavSink
from ....settings import app_settings

router = APIRouter(prefix="/ws", tags=["interviews"])

DEFAULT_STT_ENDPOINT = "wss://api.openai.com"


def _transcription_url() -> str | None:
    """Build the upstream realtime URL, or None if STT is not configured.

    The model must NOT be a query parameter for transcription sessions; it is
    set via session.update in the handshake (see _init_transcript).
    """
    speech_settings = app_settings.services.speech
    if speech_settings.stt_model is None:
        return None
    endpoint = (speech_settings.stt_endpoint or DEFAULT_STT_ENDPOINT).rstrip("/")
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


async def _await_session_response(upstream) -> dict:
    """Consume upstream events until a session.update is acknowledged."""
    while True:
        event = json.loads(await asyncio.wait_for(upstream.recv(), timeout=10))
        if event.get("type") in ("session.updated", "error"):
            return event


async def _init_transcript(
    upstream,
    stt_model: str,
    language: LanguageCode | None,
    last_message: str | None,
) -> None:
    """Configure the upstream transcription session: PCM16 input, no server
    VAD (the client commits the buffer manually on send), and the interview
    language. Raises WSException if this core configuration is rejected.

    The last interviewer question is then added as a transcription prompt in a
    second, best-effort update: not all models support `prompt`, and a session
    update fails atomically, so it must not ride along with the core config.
    """
    transcription: dict = {
        "model": stt_model,
        "delay": app_settings.services.speech.sst_delay,
    }
    if language is not None:
        transcription["language"] = language.lower()

    await upstream.send(json.dumps(_session_update(transcription)))
    event = await _await_session_response(upstream)
    if event.get("type") == "error":
        raise WSException(f"Transcription session config rejected: {event['error']}")

    if last_message:
        prompted = transcription | {"prompt": "Q: " + last_message + "\nA:"}
        await upstream.send(json.dumps(_session_update(prompted)))
        event = await _await_session_response(upstream)
        if event.get("type") == "error":
            logger.warning(
                "Transcription prompt not applied: "
                f"{event['error'].get('message', event['error'])}"
            )


@router.websocket("/transcribe")
async def transcription_websocket_endpoint(websocket: WebSocket, db: DBSession):
    """Tee participant audio: persist the recording (source of truth) while
    forwarding it to the OpenAI-compatible transcription service best-effort.

    The server configures the upstream session (model, language, and the last
    interviewer question as prompt context). The client sends raw PCM16 binary
    frames plus the buffer-commit control message; transcripts are relayed
    straight back, and the client submits the final text through the normal
    interview message path.
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
    recording_filename = f"recording-{int(time.time())}.wav"
    sink = LocalWavSink(audio_dir / recording_filename)

    # Tell the client which file this session records to, so it can reference
    # the recording when submitting the transcribed message.
    await websocket.send_json({"type": "recording", "filename": recording_filename})

    upstream = None
    relay_task = None
    transcription_up = False

    transcription_url = _transcription_url()
    headers = {
        "Authorization": "Bearer "
        + lib_settings.secrets.openai_api_key.get_secret_value(),
        "OpenAI-Safety-Identifier": str(interview_token.interview_id),
    }

    # Session context: the interview language and the question being answered.
    language: LanguageCode | None = None
    last_message: str | None = None
    try:
        interview = db.interviews.get_interview(
            project_id=interview_token.project_id,
            interview_id=interview_token.interview_id,
        )
        language = interview.language
        last = db.interviews.get_last_message(
            interview_id=interview_token.interview_id,
            project_id=interview_token.project_id,
            role=MessageRole.ASSISTANT,
        )
        if last is not None:
            # Prompts condition on their tail; keep the end of long questions.
            last_message = last.content.strip()[-1000:] or None
    except NoResultFound:
        logger.error(f"Interview {interview_token.interview_id} not found")

    try:
        if (
            transcription_url is None
            or (stt_model := app_settings.services.speech.stt_model) is None
        ):
            logger.error("Transcription not configured (services.speech)")
            await _notify(websocket, "transcription_unavailable")
        else:
            try:
                upstream = await websockets.connect(
                    transcription_url, additional_headers=headers, max_size=None
                )
                await _init_transcript(upstream, stt_model, language, last_message)
                transcription_up = True
                relay_task = asyncio.create_task(
                    _relay_transcripts(websocket, upstream)
                )
            except (OSError, WSException, TimeoutError) as e:
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
