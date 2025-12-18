import json

from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute

from . import auth, interview, misc
from .admin import main as admin
from .dashboard import main as dashboard
from .synthesize import main as synthesize

router = APIRouter(
    prefix="/api",
)

router.include_router(dashboard.router)
router.include_router(interview.router)
router.include_router(auth.router)
router.include_router(synthesize.router)
router.include_router(admin.router)
router.include_router(misc.router)


@router.get("/health")
def health():
    return "success"


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
