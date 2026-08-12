from sqlalchemy.exc import NoResultFound
import json

from fastapi import APIRouter, FastAPI, Request
from fastapi.routing import APIRoute

from ..dependencies import DBSession
from ..openapi import build_openapi_schema
from ..platform_release import PlatformManifest, PlatformRelease
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


@router.get("/openapi.json", include_in_schema=False)
def openapi_schema(request: Request):
    """The schema the frontend generates its SDK from (`just generate-sdk`).

    Not FastAPI's own `/openapi.json`: see `app.openapi` for what differs.
    Excluded from the schema itself so it does not become an SDK operation.
    """
    return build_openapi_schema(request.app)


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


@router.get("/releases")
def releases(db: DBSession, limit: int = 10) -> list[PlatformRelease]:
    """Recent releases for the "What's new" dialog.

    Only the curated, user-facing view: per-component `notes` and git hashes
    stay out of it. A release that has not been curated yet (`highlights` is
    None) is omitted rather than shown empty.
    """
    return [
        PlatformRelease(
            platform_version=manifest.platform_version,
            released_at=manifest.build_time,
            highlights=manifest.highlights or [],
        )
        for manifest in db.get_platform_releases(limit=limit)
        if manifest.highlights is not None
    ]


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
