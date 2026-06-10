import wave
from pathlib import Path
from typing import Protocol

# OpenAI transcription input format: 24 kHz mono PCM16.
SAMPLE_RATE = 24_000
CHANNELS = 1
SAMPLE_WIDTH = 2


class AudioSink(Protocol):
    """A destination for streamed PCM audio. The recording is the source of
    truth, so writes must not depend on transcription succeeding."""

    def write(self, pcm: bytes) -> None: ...

    def close(self) -> None: ...


class LocalWavSink:
    """Streams 24 kHz mono PCM16 frames to a .wav on the local filesystem.

    `wave` patches the RIFF header sizes on close(), so a recording is still
    finalized when a connection drops, as long as close() runs.
    """

    def __init__(self, path: Path):
        self.path = path
        self._wav = wave.open(str(path), "wb")
        self._wav.setnchannels(CHANNELS)
        self._wav.setsampwidth(SAMPLE_WIDTH)
        self._wav.setframerate(SAMPLE_RATE)

    def write(self, pcm: bytes) -> None:
        self._wav.writeframes(pcm)

    def close(self) -> None:
        self._wav.close()
