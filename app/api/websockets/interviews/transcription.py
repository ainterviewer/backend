import asyncio
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.exc import NoResultFound
from uvicorn.config import logger

from ainterviewer.settings import settings as lib_settings
from ainterviewer.types import LanguageCode, MessageRole

from ....dependencies import DBSession
from ....services.audio import (
    AudioServiceError,
    LocalWavSink,
    TranscriptionSession,
)
from ..auth import authenticate_or_close

router = APIRouter(prefix="/ws", tags=["interviews"])

# Prompts condition on their tail; keep the end of long questions.
PROMPT_CONTEXT_CHARS = 1000


async def _notify(websocket: WebSocket, error: str) -> None:
    try:
        await websocket.send_json({"type": "error", "error": error})
    except Exception:
        logger.debug("Could not notify client of error %r; socket is gone", error)


async def _relay_transcripts(
    websocket: WebSocket, session: TranscriptionSession
) -> None:
    """Forward transcription events from the upstream session to the client."""
    async for message in session.events():
        if isinstance(message, bytes):
            await websocket.send_bytes(message)
        else:
            await websocket.send_text(message)


@router.websocket("/transcribe")
async def transcription_websocket_endpoint(websocket: WebSocket, db: DBSession):
    """Tee participant audio: persist the recording (source of truth) while
    forwarding it to the transcription service best-effort.

    The client sends raw PCM16 binary frames plus the buffer-commit control
    message; transcripts are relayed straight back, and the client submits the
    final text through the normal interview message path. Everything about the
    upstream protocol lives in `services.audio`.
    """
    interview_token = await authenticate_or_close(websocket)
    if interview_token is None:
        return

    audio_dir = lib_settings.storage.interview_storage.audio_path(
        interview_token.interview_id
    )
    recording_filename = f"recording-{int(time.time())}.wav"
    sink = LocalWavSink(audio_dir / recording_filename)

    # Tell the client which file this session records to, so it can reference
    # the recording when submitting the transcribed message.
    await websocket.send_json({"type": "recording", "filename": recording_filename})

    # Session context: the interview language and the question being answered.
    language: LanguageCode | None = None
    prompt_context: str | None = None
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
            prompt_context = last.content.strip()[-PROMPT_CONTEXT_CHARS:] or None
    except NoResultFound:
        logger.error(f"Interview {interview_token.interview_id} not found")

    session: TranscriptionSession | None = None
    relay_task: asyncio.Task | None = None
    try:
        try:
            session = await TranscriptionSession.open(
                interview_token.interview_id,
                language=language,
                prompt_context=prompt_context,
            )
            relay_task = asyncio.create_task(_relay_transcripts(websocket, session))
        except AudioServiceError as e:
            logger.error(f"Transcription unavailable: {e}")
            await _notify(websocket, "transcription_unavailable")

        while True:
            msg = await websocket.receive()
            if msg["type"] == "websocket.disconnect":
                break

            if (pcm := msg.get("bytes")) is not None:
                sink.write(pcm)  # unconditional: recording is the source of truth
                if session is not None and session.is_open:
                    try:
                        await session.append_audio(pcm)
                    except AudioServiceError as e:
                        logger.error(f"Transcription dropped: {e}")
                        await _notify(websocket, "transcription_unavailable")
            elif (
                (text := msg.get("text")) is not None
                and session is not None
                and session.is_open
            ):
                # Control passthrough (session.update, commit, ...).
                try:
                    await session.send_control(text)
                except AudioServiceError as e:
                    logger.error(f"Transcription dropped: {e}")
                    await _notify(websocket, "transcription_unavailable")

    except WebSocketDisconnect:
        pass

    finally:
        if relay_task is not None:
            relay_task.cancel()
        if session is not None:
            await session.close()
        sink.close()
