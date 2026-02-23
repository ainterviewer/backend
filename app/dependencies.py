from pathlib import Path
from typing import Annotated, Literal

from fastapi import Cookie, Depends, HTTPException
from fastapi.security import APIKeyCookie
from fastapi.templating import Jinja2Templates
from jose import JWTError
from pydantic import UUID4, ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ainterviewer.types import LanguageCode

from .auth import AuthToken, decode_auth_token, AssistanceSessionToken
from .db import InterviewDataBase
from .db.vectors import register_vector_extension
from .settings import app_settings
from .types import CollaboratorRole, Scope
from .websockets import WebSocketConnectionManager

# Templating
fronted_dir = Path(__file__).parent.absolute() / "frontend"

templates_dir = fronted_dir / "templates"
templates = Jinja2Templates(directory=templates_dir)

auth_cookie_scheme = APIKeyCookie(name="token", auto_error=False)

# oauth2_scheme = OAuth2PasswordBearer(
#     tokenUrl="token",
#     scopes={
#         "me": "Read information about the current user.",
#         "guest": "Browse the site as a guest.",
#         "admin": "Perform administrative tasks.",
#     },
# )


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


def get_db():
    with Session(engine) as session:
        db = InterviewDataBase(session)
        yield db


manager = WebSocketConnectionManager()


def get_ws_manager():
    return manager


class AuthError(HTTPException): ...


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
        token: str | None = Depends(auth_cookie_scheme),
    ) -> AuthToken:
        # TODO:
        # Should this also check is the user_id is in the database?
        try:
            if token is None:
                raise AuthError(status_code=401, detail="Not authenticated")

            try:
                auth_token = decode_auth_token(token)
                scope_strings = set(auth_token.scope.split())
                user_scopes = {Scope(scope) for scope in scope_strings}

                if not self.has_required_scope(user_scopes):
                    raise AuthError(
                        status_code=403,
                        detail="Forbidden, scope required: " + self.required_scope,
                    )
                return auth_token
            except (JWTError, ValidationError):
                raise AuthError(
                    status_code=403, detail="Could not validate credentials"
                )
        except AuthError as e:
            print(e)
            raise e


class ResourceRoleChecker:
    """Check user has required CollaboratorRole on a resource."""

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
        token: AuthToken = Depends(ScopeChecker(Scope.USER)),
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
            print(self.required_role, self.resource_type)
            print(project_id, folder_id)
            raise HTTPException(400, "Missing resource identifier")

        if user_role is None:
            raise HTTPException(404, "Resource not found")

        if not user_role.includes(self.required_role):
            raise HTTPException(403, f"Requires {self.required_role} role")

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

# User tokens
AdminToken = Annotated[AuthToken, Depends(ScopeChecker(Scope.ADMIN))]
UserToken = Annotated[AuthToken, Depends(ScopeChecker(Scope.USER))]
DemoToken = Annotated[AuthToken, Depends(ScopeChecker(Scope.DEMO))]
GuestToken = Annotated[AuthToken, Depends(ScopeChecker(Scope.GUEST))]


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
