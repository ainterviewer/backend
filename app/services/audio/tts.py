"""Text-to-speech: synthesis of interviewer messages as streamed MP3.

The endpoint hands over text plus the interview language and gets back a byte
stream; the upstream request, its voice/pacing settings and the session
lifetime all live here.
"""

from collections.abc import AsyncIterator

import aiohttp
from uvicorn.config import logger

from ainterviewer.settings import settings as lib_settings
from ainterviewer.types import LanguageCode

from ...settings import app_settings
from .errors import SpeechNotConfigured, SpeechUnavailable

DEFAULT_TTS_ENDPOINT = "https://api.openai.com"

# OpenAI's /v1/audio/speech caps input at 4096 characters.
MAX_INPUT_CHARS = 4096

CHUNK_SIZE = 8192

# gpt-4o-mini-tts steers delivery via instructions; `speed` is only honored by
# the older tts-1 models, so pacing is stated in both places.
TTS_SPEED = 1.15
TTS_INSTRUCTIONS = (
    "You are the voice of a friendly, professional researcher reading "
    "interview questions aloud to a participant. Tone: warm, engaged and "
    "natural, never robotic or theatrical. Pacing: slightly faster than "
    "normal conversation while staying clear and easy to follow; pause "
    "briefly after a question. Language: the text is in the language with "
    "ISO 639-1 code '{language}'; pronounce it like a native speaker."
)


async def synthesize(text: str, language: LanguageCode) -> AsyncIterator[bytes]:
    """Synthesize `text` and return the MP3 as it is generated.

    Awaiting this performs the upstream request, so a misconfigured or failing
    service raises here -- before the caller has committed to a response body.
    The returned iterator owns the aiohttp session and closes it when the
    stream is exhausted or the consumer goes away.

    Raises `SpeechNotConfigured` when no TTS model is set, and
    `SpeechUnavailable` when the upstream is unreachable or errors.
    """
    speech_settings = app_settings.services.speech
    if speech_settings.tts_model is None:
        raise SpeechNotConfigured("Text-to-speech not configured (services.speech)")

    endpoint = (speech_settings.tts_endpoint or DEFAULT_TTS_ENDPOINT).rstrip("/")

    session = aiohttp.ClientSession()
    try:
        upstream = await session.post(
            f"{endpoint}/v1/audio/speech",
            headers={
                "Authorization": "Bearer "
                + lib_settings.secrets.openai_api_key.get_secret_value(),
            },
            json={
                "model": speech_settings.tts_model,
                "voice": speech_settings.tts_voice,
                "input": text[:MAX_INPUT_CHARS],
                "response_format": "mp3",
                "speed": TTS_SPEED,
                "instructions": TTS_INSTRUCTIONS.format(language=language),
            },
        )
    except aiohttp.ClientError as e:
        await session.close()
        raise SpeechUnavailable(f"TTS upstream unavailable: {e!r}")

    if upstream.status != 200:
        detail = await upstream.text()
        upstream.release()
        await session.close()
        raise SpeechUnavailable(f"TTS upstream error {upstream.status}: {detail}")

    async def stream() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.content.iter_chunked(CHUNK_SIZE):
                yield chunk
        except aiohttp.ClientError as e:
            # Mid-stream: the response is already in flight, so there is no
            # status left to change -- log it and end the body short.
            logger.error(f"TTS stream interrupted: {e!r}")
        finally:
            upstream.release()
            await session.close()

    return stream()
