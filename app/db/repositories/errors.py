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


class CommentThreadError(Exception):
    """A comment could not be placed in the thread it named.

    Raised when a reply names a parent that is itself a reply (threads are two
    levels deep) or a parent on a different message. The API maps this to a
    400: it means the caller built the wrong parent, and silently re-pointing
    the reply at the root would hide that.
    """


class CodebookError(Exception):
    """A codebook could not be saved as sent.

    Raised when the payload is not a tree over its own codes -- a parent that
    is not in it, a cycle, a duplicated id -- or when saving it would throw
    away coding already done, which is the one case where the user has to
    decide rather than the server. The API maps this to a 400 and shows the
    message, so keep messages user-facing.
    """


class CodingError(Exception):
    """A passage could not be coded the way the caller asked.

    Raised for a code that is a group (groups are never applied), a score
    without a value or with one outside its range, a span that does not fall
    inside the message, and a coding that already exists. The API maps this to
    a 400 and shows the message.
    """
