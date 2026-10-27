import io

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import UUID4
from sqlalchemy.exc import NoResultFound

from ainterviewer.settings import settings as lib_settings

from ...auth import AuthToken
from ...dependencies import DBSession, UserToken
from ...schemas.projects import ExperimentCreate, ExperimentPublic
from ...types import CollaboratorRole, Scope
from ...utils import generate_qr_img
from ..files import file_response

router = APIRouter(tags=["experiments"])


@router.get("/experiments")
async def get_experiments(
    db: DBSession,
    jwt: UserToken,
) -> list[ExperimentPublic]:
    return db.tests.get_experiments(user_id=jwt.user_id)


def _require_editor_on_projects(
    db: DBSession, jwt: AuthToken, project_ids: list[UUID4]
) -> None:
    """An experiment sends respondents into each of its projects, so linking
    one takes the same editor role as distributing it.

    The projects are in the body rather than the path, so no role dependency
    covers them. Admins skip the role check but not the existence check: a
    missing project would otherwise surface as a foreign-key 500.
    """
    is_admin = Scope.ADMIN in {Scope(s) for s in jwt.scope.split()}
    for project_id in set(project_ids):
        if is_admin:
            if not db.projects.project_exists(project_id):
                raise HTTPException(404, "Resource not found")
            continue
        role = db.projects.get_user_role_on_project(jwt.user_id, project_id)
        if role is None:
            raise HTTPException(404, "Resource not found")
        if not role.includes(CollaboratorRole.EDITOR):
            raise HTTPException(403, "Requires editor role")


@router.post("/experiments")
async def create_experiment(
    experiment: ExperimentCreate,
    db: DBSession,
    jwt: UserToken,
) -> ExperimentPublic:
    _require_editor_on_projects(db, jwt, [p.project_id for p in experiment.projects])
    return db.tests.create_experiment(experiment, user_id=jwt.user_id)


@router.delete("/experiments/{experiment_id}")
async def delete_experiment(
    experiment_id: UUID4,
    db: DBSession,
    jwt: UserToken,
    clear_experiment_only: bool = False,
) -> None:
    """`clear_experiment_only` lets the experiment's projects take respondents
    from their own links again, unless another experiment still uses them."""
    return db.tests.delete_experiment(
        experiment_id, user_id=jwt.user_id, clear_experiment_only=clear_experiment_only
    )


@router.get(
    "/experiments/{experiment_id}/qr.png",
    response_class=Response,
    responses=file_response("image/png"),
)
async def generate_experiment_qr(
    request: Request,
    experiment_id: UUID4,
    jwt: UserToken,
    db: DBSession,
):
    # Experiments are their creator's alone, as the list and delete are. This
    # also stops a QR file being written for every id anyone cares to send.
    try:
        db.tests.get_experiment(experiment_id, user_id=jwt.user_id)
    except NoResultFound:
        raise HTTPException(404, "Experiment not found")

    file_path = (
        lib_settings.storage.experiment_storage.qr_code_path(experiment_id)
        / "distribute.png"
    )

    if not file_path.exists():
        redirect_url = str(request.base_url) + f"interview/redirect?id={experiment_id}"
        img_data = generate_qr_img(str(redirect_url), file_path=file_path)

        return StreamingResponse(io.BytesIO(img_data), media_type="image/png")

    return FileResponse(file_path)
