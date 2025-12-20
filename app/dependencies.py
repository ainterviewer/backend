from pathlib import Path
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.security import APIKeyCookie
from fastapi.templating import Jinja2Templates
from jinjax import Catalog, JinjaX
from jose import JWTError
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ainterviewer.types import LanguageCode

from .auth import AuthToken, InterviewToken, decode_auth_token
from .db import InterviewDataBase
from .settings import app_settings
from .types import Scope
from .websockets import WebSocketConnectionManager

# Templating
fronted_dir = Path(__file__).parent.absolute() / "frontend"

templates_dir = fronted_dir / "templates"
templates = Jinja2Templates(directory=templates_dir)

templates.env.add_extension(JinjaX)
catalog = Catalog(jinja_env=templates.env)
catalog.add_folder(templates_dir / "site" / "interview" / "components")

auth_cookie_scheme = APIKeyCookie(name="token", auto_error=False)

# oauth2_scheme = OAuth2PasswordBearer(
#     tokenUrl="token",
#     scopes={
#         "me": "Read information about the current user.",
#         "guest": "Browse the site as a guest.",
#         "admin": "Perform administrative tasks.",
#     },
# )


engine = create_engine(
    app_settings.database.connection_string,
    pool_size=20,
    max_overflow=40,
)
# TODO:
# - Should probably be async instead
# - get_db should yield a session instead, update crud manager accordingly,
# greatly reduces the number of required lines of code
#
# NOTE:
# Pragmas implemented in create_db_and_tables


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


manager = WebSocketConnectionManager()


def get_ws_manager():
    return manager


def get_db():
    with Session(engine) as session:
        db = InterviewDataBase(session)
        yield db


DBSession = Annotated[InterviewDataBase, Depends(get_db)]
AdminToken = Annotated[AuthToken, Depends(ScopeChecker(Scope.ADMIN))]
UserToken = Annotated[AuthToken, Depends(ScopeChecker(Scope.USER))]
GuestToken = Annotated[AuthToken, Depends(ScopeChecker(Scope.GUEST))]
LocalizationCookie = Annotated[LanguageCode, Cookie(alias="localization")]
LanguageCookie = Annotated[LanguageCode, Cookie(alias="language")]
