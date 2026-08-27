"""Speech services: recording sinks plus the speech-to-text and text-to-speech
clients that the interview endpoints drive.

The upstream protocol details (realtime handshake, PCM framing, the audio
/speech request) live here; the endpoints only own their own socket or HTTP
response.
"""

from .errors import AudioServiceError, SpeechNotConfigured, SpeechUnavailable
from .sinks import CHANNELS, SAMPLE_RATE, SAMPLE_WIDTH, AudioSink, LocalWavSink
from .stt import TranscriptionSession
from .tts import synthesize

__all__ = [
    "CHANNELS",
    "SAMPLE_RATE",
    "SAMPLE_WIDTH",
    "AudioServiceError",
    "AudioSink",
    "LocalWavSink",
    "SpeechNotConfigured",
    "SpeechUnavailable",
    "TranscriptionSession",
    "synthesize",
]
