# TODO: Fully implement suggestions from this guide, both in creation of the
# SDK and afterwards implement it in frontend
# https://fastapi.tiangolo.com/advanced/generate-clients/#custom-generate-unique-id-function

import logging

import rich.console
import rich.logging
import rich.theme
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from . import __version__
from .api import main as api
from .api import ws
from .dependencies import AuthError, templates
from .settings import app_settings

# =========== #
# Init Logger #
# =========== #

custom_theme = rich.theme.Theme(
    {
        "info": "cyan",
        "warning": "yellow",
        "error": "bold red",
        "critical": "bold white on red",
    }
)
console = rich.console.Console(theme=custom_theme)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

rich_handler = rich.logging.RichHandler(
    console=console,
    show_time=True,
    show_path=False,
    rich_tracebacks=True,
    tracebacks_show_locals=True,
)

rich_handler.setFormatter(logging.Formatter(" %(message)s"))
logger.addHandler(rich_handler)

# ============ #
# Init FastAPI #
# ============ #

app = FastAPI(title="AInterviewer", version=__version__)

app.add_middleware(
    SessionMiddleware,
    secret_key=app_settings.secrets.session_secret_key.get_secret_value(),
    max_age=6000,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[app_settings.sveltekit_addr],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api.router)
app.include_router(ws.router)


@app.exception_handler(ValueError)
async def custom_exception_handler(request: Request, exc: Exception):
    logger.error(f"ValueError occurred: {str(exc)}")

    if request.url.path.startswith("/api"):
        raise exc

    # FIXME: This doesnt work with new SvelteKit frontend
    return templates.TemplateResponse(
        "error.html", {"request": request, "error_message": str(exc)}, status_code=500
    )


@app.exception_handler(AuthError)
async def http_redirect(request: Request, exc: AuthError):
    if request.url.path.startswith("/api"):
        raise exc
    if exc.status_code == 401 or exc.status_code == 403:
        return RedirectResponse(url="/login")
    raise exc


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=app_settings.app.ainterviewer_port,
        reload=True,
        reload_dirs=["."],
        reload_includes=["*.html", "*.jinja", "*.yaml"],
        reload_excludes=["test_*.py"],
    )
