from sqlalchemy.exc import NoResultFound
import json

from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute

from ..dependencies import DBSession
from ..platform_release import PlatformManifest
from . import auth, interview, misc
from .admin import main as admin
from .dashboard import main as dashboard

router = APIRouter(
    prefix="/api",
)

router.include_router(dashboard.router)
router.include_router(interview.router)
router.include_router(auth.router)
router.include_router(admin.router)
router.include_router(misc.router)


@router.get("/health")
def health():
    return "success"


@router.get("/version")
def version(db: DBSession) -> PlatformManifest | None:
    try:
        return db.get_platform_release()
    except NoResultFound:
        return None


@router.get("/version/{platform_version}")
def platform_version(db: DBSession, platform_version: str):
    return db.get_platform_release(platform_version=platform_version)


def use_route_names_as_operation_ids(router: APIRouter) -> None:
    """
    Simplify operation IDs so that generated API clients have simpler function
    names.

    Should be called only after all routes have been added.
    """
    for route in router.routes:
        if isinstance(route, APIRoute):
            route.operation_id = route.name


use_route_names_as_operation_ids(router)

if __name__ == "__main__":
    app = FastAPI()
    app.include_router(router)
    with open("openapi.json", "w") as f:
        f.write(json.dumps(app.openapi(), indent=4))
