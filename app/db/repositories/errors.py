class ProjectLanguageError(Exception):
    """A localization operation would break a project's language invariants.

    Raised when a project would be left without a localization, or without a
    default one. The API layer maps this to a 409 and shows the message to the
    user, so keep messages user-facing.
    """


class ContentRevisionError(Exception):
    """A write to a project's content was based on a revision that is no
    longer current: someone else saved in between.

    `part` is the `ContentPart` whose revision was checked, or None for the
    overall one (publish, restore). The API maps this to a 412 carrying
    `current` and `part`, so the editor can offer to reload or to overwrite
    with that revision.
    """

    def __init__(self, expected: int, current: int, part: str | None = None):
        super().__init__(
            f"The {part or 'content'} changed since it was loaded "
            f"(revision {expected}, now {current})."
        )
        self.expected = expected
        self.current = current
        self.part = part


class NotPublishedError(Exception):
    """Respondents asked for a project that has never been published.

    Distributed interviews, the public consent/welcome/language routes and
    participant emails read the published version only, and never fall back to
    the draft. The API maps this to a 409, so keep messages user-facing.
    """


class PublishError(Exception):
    """The draft cannot be published as it stands.

    Raised while an offered language has untranslated text; `missing` maps
    each such language to the paths of what it lacks. The API maps this to a
    409 and shows the message.
    """

    def __init__(self, message: str, missing: dict[str, list[str]]):
        super().__init__(message)
        self.missing = missing


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


class OptOutTokenError(Exception):
    """An opt-out link did not resolve to a participant.

    Raised for a token that does not decode and for one that names nobody --
    typically a participant deleted after their email went out, or a link an
    email client wrapped or truncated. The API turns both into the same 404,
    since to the participant following it they are the same broken link.

    ``reason`` is for the server log, never for the response body.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class InterviewGoneError(Exception):
    """A write for an interview failed because the interview no longer exists.

    Raised from the interview loop's own writes (messages and tasks) when the
    foreign-key check rejects them: the interview was deleted while its session
    was still running. Deleting endpoints end the session first (see
    `InterviewSessionManager.end`), so this is the backstop for whatever slips
    past that. The websocket maps it to the restart frame a reconnect to a
    deleted interview gets.
    """

    def __init__(self, interview_id):
        super().__init__(f"interview {interview_id} no longer exists")
        self.interview_id = interview_id


class InvitationAliasTakenError(Exception):
    """Another invitation already has this alias. The API answers 409."""

    def __init__(self, alias: str):
        super().__init__(f"The alias {alias!r} is already in use")
        self.alias = alias
