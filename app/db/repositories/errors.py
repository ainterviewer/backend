class ProjectLanguageError(Exception):
    """A localization operation would break a project's language invariants.

    Raised when a project would be left without a localization, or without a
    default one. The API layer maps this to a 409 and shows the message to the
    user, so keep messages user-facing.
    """


class ResumeTokenError(Exception):
    """A resume link could not be redeemed.

    Raised for every rejection -- unknown, revoked, already redeemed, expired,
    interview already completed -- because the API turns all of them into the
    same opaque 404. Distinguishing them to the caller would tell someone
    probing tokens which guesses were real.

    ``reason`` is for the server log, never for the response body.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason
