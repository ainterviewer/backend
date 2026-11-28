"""The exception the API answers with an `ApiError` body.

Raise an `AppError` -- or a subclass, which is what the domain exceptions in
`app.db.repositories.errors` are -- anywhere below a route, and the handler
`add_error_handlers` installs answers it: `status`, a body of `detail` (the message), `code`
and whatever `extra` fields it carries, and any `headers`. Routes therefore
never catch a domain error only to re-raise it as an `HTTPException`.

A plain `HTTPException` still works and comes out in the same shape with a
null `code`; reach for `AppError` when a client has to tell the error apart.
"""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.utils import is_body_allowed_for_status_code
from starlette.exceptions import HTTPException as StarletteHTTPException

from ainterviewer.exceptions import ModelUnavailable, UnavailableReason

from .types import ErrorCode


class AppError(Exception):
    status: int = 400
    code: ErrorCode | None = None

    def __init__(
        self,
        detail: str,
        *,
        status: int | None = None,
        code: ErrorCode | None = None,
        headers: dict[str, str] | None = None,
        **extra: Any,
    ):
        super().__init__(detail)
        self.detail = detail
        if status is not None:
            self.status = status
        if code is not None:
            self.code = code
        self.headers = headers
        self.extra = extra

    def body(self) -> dict[str, Any]:
        return {"detail": self.detail, "code": self.code, **self.extra}


def add_error_handlers(app: FastAPI) -> None:
    """Answer every error with an `ApiError` body (app/schemas/common.py), so
    a client reads one shape whatever raised it. Only FastAPI's own 422 keeps
    its list of validation errors.

    A function rather than handlers declared on the app in `app/main.py` so a
    test's throwaway app can answer errors the way the real one does.
    """

    @app.exception_handler(AppError)
    async def _app_error(_request: Request, exc: AppError):
        return JSONResponse(exc.body(), status_code=exc.status, headers=exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException):
        if not is_body_allowed_for_status_code(exc.status_code):
            return Response(status_code=exc.status_code, headers=exc.headers)
        return JSONResponse(
            {"detail": str(exc.detail), "code": None},
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @app.exception_handler(ModelUnavailable)
    async def _model_unavailable(_request: Request, exc: ModelUnavailable):
        # Whatever route called a model that is not there -- a translation, a
        # generated guide -- says so, rather than "something went wrong". The
        # interview websocket waits these out itself; an HTTP request is the
        # caller's to retry, when `Retry-After` says it is worth it.
        detail = (
            "The language model is starting up. Please try again in a minute."
            if exc.reason is UnavailableReason.STARTING
            else "The language model is not available right now. Please try again later."
            if exc.retry_after is None
            else "The language model is busy. Please try again shortly."
        )
        headers = (
            {"Retry-After": str(exc.retry_after)}
            if exc.retry_after is not None
            else None
        )
        return JSONResponse(
            {"detail": detail, "code": ErrorCode.MODEL_UNAVAILABLE},
            status_code=503,
            headers=headers,
        )

    @app.exception_handler(Exception)
    async def _internal_error(_request: Request, _exc: Exception):
        # Starlette logs the exception and re-raises it after this; the handler
        # only decides the body, which would otherwise be plain text.
        return JSONResponse(
            {"detail": "Something went wrong on our side.", "code": ErrorCode.INTERNAL},
            status_code=500,
        )
