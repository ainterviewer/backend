class ProjectLanguageError(Exception):
    """A localization operation would break a project's language invariants.

    Raised when a project would be left without a localization, or without a
    default one. The API layer maps this to a 409 and shows the message to the
    user, so keep messages user-facing.
    """
