import datetime
import logging
import threading
from collections.abc import Generator
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Cookie, Depends, Header, HTTPException, Response
from fastapi.security import APIKeyCookie
from jose import JWTError
from pydantic import UUID4, ValidationError
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ainterviewer.types import LanguageCode
from ainterviewer.utils import now

from .auth import AssistanceSessionToken, AuthToken, InterviewToken
from .db import InterviewDataBase
from .db.pragmas import register_pragmas
from .db.regexp import register_regexp
from .db.triggers import register_trigger_functions
from .db.vectors import register_vector_extension
from .settings import app_settings
from .types import CollaboratorRole, Scope

# from .websockets import WebSocketConnectionManager


logger = logging.getLogger(__name__)


class AuthError(HTTPException): ...


auth_cookie_scheme = APIKeyCookie(name="access_token", auto_error=False)


# TODO:
# - Should probably be async instead
# - get_db should yield a session instead, update crud manager accordingly,
# greatly reduces the number of required lines of code
# - encrypt data with SQLCipher
engine = create_engine(
    app_settings.database.connection_string,
    pool_size=20,
    max_overflow=40,
)

register_vector_extension(engine)
register_pragmas(
    engine, enforce_foreign_keys=app_settings.database.enforce_foreign_keys
)
register_regexp(engine)
register_trigger_functions(engine)


def get_db() -> Generator[InterviewDataBase, None, None]:
    with Session(engine) as session:
        db = InterviewDataBase(session)
        yield db


# manager = WebSocketConnectionManager()
#
#
# def get_ws_manager():
#     return manager


def _decode_token(
    token: str | None = Depends(auth_cookie_scheme),
) -> AuthToken:
    if token is None:
        raise AuthError(status_code=401, detail="Not authenticated")
    try:
        return AuthToken.decode(token)
    except (JWTError, ValidationError):
        raise AuthError(status_code=401, detail="Could not validate credentials")


def _decode_optional_token(
    token: str | None = Depends(auth_cookie_scheme),
) -> AuthToken | None:
    """The caller's token if they sent a valid one, otherwise None.

    For endpoints that serve everyone but tailor the response to who is asking.
    An expired or malformed token is treated as no token rather than refused.
    """
    if token is None:
        return None
    try:
        return AuthToken.decode(token)
    except (JWTError, ValidationError):
        return None


# When this process last recorded each user's activity. Per-process like the
# session registry: with several workers each records once per debounce, which
# only costs a few extra idempotent upserts.
_activity_recorded: dict[UUID, datetime.datetime] = {}
_activity_lock = threading.Lock()


def _record_activity(
    auth_token: AuthToken = Depends(_decode_token),
    db: InterviewDataBase = Depends(get_db),
) -> AuthToken:
    """Note that the caller used the dashboard today; see `UserActivityTable`.

    Every scope- or role-checked endpoint passes through here (`ScopeChecker`,
    `ResourceRoleChecker`), so this is what feeds the admin overview's
    active-user counts and `user.last_active`. The in-process cache keeps it
    off the database for all but the first request of a day and one per
    `last_active_debounce` after that. `/refresh` does not come through here,
    so a backgrounded tab refreshing its token does not count as activity.

    A failed write is logged and swallowed: losing a sample of activity must
    never cost the user their request. The slot is given back on failure, so
    the next request tries again rather than waiting out the debounce.

    This commits the request's own session (`get_db` is cached per request),
    while dependencies are still being resolved: nothing is pending yet, but
    a dependency that runs before this one and leaves writes in the session
    would have them committed here, and objects it loaded expired.
    """
    timestamp = now()
    debounce = app_settings.app.last_active_debounce.to_timedelta()
    with _activity_lock:
        last = _activity_recorded.get(auth_token.user_id)
        if (
            last is not None
            and last.date() == timestamp.date()
            and timestamp - last < debounce
        ):
            return auth_token
        # Claimed before writing, so concurrent requests don't all write.
        _activity_recorded[auth_token.user_id] = timestamp
    try:
        db.users.record_activity(auth_token.user_id, timestamp)
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("Could not record activity for %s", auth_token.user_id)
        with _activity_lock:
            if _activity_recorded.get(auth_token.user_id) == timestamp:
                if last is None:
                    del _activity_recorded[auth_token.user_id]
                else:
                    _activity_recorded[auth_token.user_id] = last
    return auth_token


class ScopeChecker:
    def __init__(self, required_scope: Scope = Scope.ADMIN):
        self.required_scope = required_scope

    def has_required_scope(self, user_scopes: set[Scope]) -> bool:
        """Check if any of the user's scopes include the required scope."""
        return any(
            user_scope.includes(self.required_scope) for user_scope in user_scopes
        )

    def __call__(
        self,
        auth_token: AuthToken = Depends(_record_activity),
    ) -> AuthToken:
        scope_strings = set(auth_token.scope.split())
        user_scopes = {Scope(scope) for scope in scope_strings}

        if not self.has_required_scope(user_scopes):
            raise AuthError(
                status_code=403,
                detail="Insufficient permissions",
            )
        return auth_token


class ResourceRoleChecker:
    """Check user has required CollaboratorRole on a resource.

    This checker only validates the user's role on the resource.
    Scope checking should be handled separately at the endpoint level
    using scope aliases (e.g. UserToken, DemoToken).

    Usage at endpoints:
        async def my_endpoint(
            jwt: UserToken,      # scope check
            _: FolderAdmin,      # role check
            db: DBSession,
        ): ...
    """

    def __init__(
        self,
        required_role: CollaboratorRole,
        resource_type: Literal["project", "folder"],
    ):
        self.required_role = required_role
        self.resource_type = resource_type

    def __call__(
        self,
        project_id: UUID4 | None = None,
        folder_id: UUID4 | None = None,
        # Several routes (analysis, synthesis, assistance) are gated by role
        # alone, so activity is recorded here as well as in `ScopeChecker`.
        token: AuthToken = Depends(_record_activity),
        db: InterviewDataBase = Depends(get_db),
    ) -> AuthToken:
        # Admin scope bypasses resource checks
        if Scope.ADMIN in {Scope(s) for s in token.scope.split()}:
            return token

        if self.resource_type == "project" and project_id:
            user_role = db.projects.get_user_role_on_project(token.user_id, project_id)
        elif self.resource_type == "folder" and folder_id:
            user_role = db.projects.get_user_role_on_folder(token.user_id, folder_id)
        else:
            raise HTTPException(400, "Missing resource identifier")

        if user_role is None:
            raise HTTPException(404, "Resource not found")

        if not user_role.includes(self.required_role):
            raise HTTPException(403, f"Requires {self.required_role} role")

        return token


#: The 403 detail for a caller without preview features. Distinct so the frontend
#: can tell a token minted before an admin enabled the flag -- which a refresh
#: fixes -- from any other refusal, and refresh and retry once on it.
PREVIEW_FEATURES_REQUIRED = "preview_features_required"


def require_preview_features(
    token: AuthToken = Depends(_decode_token),
) -> AuthToken:
    """Refuse callers whose token does not carry `preview_features`.

    Read from the token rather than the user row so the check costs no query;
    the claim is re-read from the database on every refresh. No admin bypass,
    matching the dashboard, which hides these features from admins too until
    the flag is set on their own account.
    """
    if not token.preview_features:
        raise AuthError(status_code=403, detail=PREVIEW_FEATURES_REQUIRED)
    return token


DBSession = Annotated[InterviewDataBase, Depends(get_db)]
LocalizationCookie = Annotated[LanguageCode, Cookie(alias="localization")]
LanguageCookie = Annotated[LanguageCode, Cookie(alias="language")]


def _parse_assistance_session(
    assistance_session: Annotated[str | None, Cookie()] = None,
) -> AssistanceSessionToken | None:
    if assistance_session is None:
        return None
    try:
        return AssistanceSessionToken.model_validate_json(assistance_session)
    except Exception:
        return None


AssistanceSessionCookie = Annotated[
    AssistanceSessionToken | None, Depends(_parse_assistance_session)
]


def _require_interview_token(
    interview_token: Annotated[str | None, Cookie()] = None,
) -> InterviewToken:
    """The respondent's credential for one interview, from its httponly cookie.

    This is what a participant in a running interview actually holds -- they
    have no user account and so no `access_token`. It carries the project and
    interview ids, which is the point: an endpoint acting inside an interview
    must take those ids from here rather than from the request, or a caller can
    name someone else's interview and act on it.
    """
    if interview_token is None:
        raise AuthError(status_code=401, detail="Unauthorized")
    try:
        return InterviewToken.decode(interview_token)
    except (JWTError, ValidationError):
        raise AuthError(status_code=401, detail="Could not validate credentials")


# The interview a respondent is in, as proven by their interview_token cookie.
InterviewCredential = Annotated[InterviewToken, Depends(_require_interview_token)]

# User tokens
AdminToken = Annotated[AuthToken, Depends(ScopeChecker(Scope.ADMIN))]
UserToken = Annotated[AuthToken, Depends(ScopeChecker(Scope.USER))]
DemoToken = Annotated[AuthToken, Depends(ScopeChecker(Scope.DEMO))]

# Feature flags. Not to be confused with DemoToken: `Scope.DEMO` is a
# restricted account tier, `with_preview_features` is early access to unfinished
# features.
PreviewFeatures = Annotated[AuthToken, Depends(require_preview_features)]

# The caller's token when there is one; never refuses.
OptionalToken = Annotated[AuthToken | None, Depends(_decode_optional_token)]


# Resource checks
ProjectViewer = Annotated[
    AuthToken, Depends(ResourceRoleChecker(CollaboratorRole.VIEWER, "project"))
]
ProjectAnnotator = Annotated[
    AuthToken, Depends(ResourceRoleChecker(CollaboratorRole.ANNOTATOR, "project"))
]
ProjectEditor = Annotated[
    AuthToken, Depends(ResourceRoleChecker(CollaboratorRole.EDITOR, "project"))
]
ProjectAdmin = Annotated[
    AuthToken, Depends(ResourceRoleChecker(CollaboratorRole.ADMIN, "project"))
]

FolderViewer = Annotated[
    AuthToken, Depends(ResourceRoleChecker(CollaboratorRole.VIEWER, "folder"))
]
FolderAnnotator = Annotated[
    AuthToken, Depends(ResourceRoleChecker(CollaboratorRole.ANNOTATOR, "folder"))
]
FolderEditor = Annotated[
    AuthToken, Depends(ResourceRoleChecker(CollaboratorRole.EDITOR, "folder"))
]
FolderAdmin = Annotated[
    AuthToken, Depends(ResourceRoleChecker(CollaboratorRole.ADMIN, "folder"))
]


def _respondent_content(
    project_id: UUID4,
    db: DBSession,
    token: OptionalToken,
    draft: bool = False,
) -> Literal["draft", "published"]:
    """Which content a public, respondent-facing route serves: the published
    version, or with `?draft=true` the draft, for a viewer of the project.

    Test interviews run the draft through the same interview page
    respondents use, so that page has to be able to ask for it; nobody else
    may, because the draft is unreleased work.
    """
    if not draft:
        return "published"
    if token is None:
        raise AuthError(status_code=401, detail="Not authenticated")
    ScopeChecker(Scope.DEMO)(auth_token=token)
    ResourceRoleChecker(CollaboratorRole.VIEWER, "project")(
        project_id=project_id, token=token, db=db
    )
    return "draft"


RespondentContent = Annotated[
    Literal["draft", "published"], Depends(_respondent_content)
]


def _if_match(if_match: Annotated[str | None, Header()] = None) -> int:
    """The content revision a write is based on, from `If-Match`.

    Required: a write that does not say what it was based on could overwrite
    anyone's work (428). The value is the `ETag` the GET returned, quoted.
    """
    if if_match is None:
        raise HTTPException(
            status_code=428,
            detail="Send the content's ETag as If-Match, so a save cannot "
            "overwrite changes you have not seen.",
        )
    try:
        return int(if_match.removeprefix("W/").strip().strip('"'))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Malformed If-Match") from exc


#: The revision a content write is based on: its part's, or the overall one
#: for publish and restore. See `ProjectContentTable.revision`.
IfMatch = Annotated[int, Depends(_if_match)]


def set_etag(response: Response, revision: int) -> None:
    """Tell the client a revision, to send back as If-Match."""
    response.headers["ETag"] = f'"{revision}"'
