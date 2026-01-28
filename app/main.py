# TODO: Fully implement suggestions from this guide, both in creation of the
# SDK and afterwards implement it in frontend
# https://fastapi.tiangolo.com/advanced/generate-clients/#custom-generate-unique-id-function
import logging
from contextlib import asynccontextmanager

import rich.console
import rich.logging
import rich.theme
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import __version__
from .api import main as api
from .api import ws
from .db import InterviewDataBase
from .dependencies import engine
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    with Session(engine) as session:
        db = InterviewDataBase(session)
        db.on_startup()

    yield

    with Session(engine) as session:
        db = InterviewDataBase(session)
        db.on_shutdown()


app = FastAPI(
    title="AInterviewer",
    version=__version__,
    lifespan=lifespan,
)

# Middleware
app.add_middleware(
    SessionMiddleware,
    secret_key=app_settings.secrets.session_secret_key.get_secret_value(),
    max_age=6000,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        app_settings.sveltekit_platform_addr,
        app_settings.sveltekit_website_addr,
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(api.router)
app.include_router(ws.router)

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=app_settings.app.api_port,
        reload=True,
        reload_dirs=["."],
        reload_includes=["*.html", "*.jinja", "*.yaml"],
        reload_excludes=["test_*.py"],
    )
