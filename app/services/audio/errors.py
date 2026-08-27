class AudioServiceError(Exception):
    """Base class for the audio service's domain errors.

    Repositories raise their own domain exceptions rather than HTTPException
    (see app/db/repositories/errors.py); the audio services follow the same
    rule so that the same client code is usable from an HTTP endpoint and from
    a websocket, which has no status codes to map onto.
    """


class SpeechNotConfigured(AudioServiceError):
    """The deployment has no model configured for this direction.

    A deployment-level fact, not a transient failure: retrying will not help
    until `services.speech` is filled in.
    """


class SpeechUnavailable(AudioServiceError):
    """The upstream speech service could not be reached, rejected the session,
    or dropped mid-stream. Transient from the caller's point of view."""
