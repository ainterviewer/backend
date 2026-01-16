import io

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import UUID4

from ...db.models import ExperimentCreate
from ...dependencies import DBSession, UserToken
from ...settings import app_settings
from ...utils import generate_qr_img

router = APIRouter(tags=["experiments"])


@router.get("/experiments")
async def get_experiments(
    db: DBSession,
    jwt: UserToken,
):
    return db.tests.get_experiments()


@router.post("/experiments")
async def create_experiment(
    experiment: ExperimentCreate,
    db: DBSession,
    jwt: UserToken,
):
    return db.tests.create_experiment(experiment)


@router.delete("/experiments/{experiment_id}")
async def delete_experiment(
    experiment_id: UUID4,
    db: DBSession,
    jwt: UserToken,
):
    return db.tests.delete_experiment(experiment_id)


@router.get("/experiments/{experiment_id}/qr.png")
async def generate_experiment_qr(
    request: Request,
    experiment_id: UUID4,
    jwt: UserToken,
):
    file_path = (
        app_settings.storage.experiment_storage.qr_code_path(experiment_id)
        / "distribute.png"
    )

    if not file_path.exists():
        redirect_url = str(request.base_url) + f"interview/redirect?id={experiment_id}"
        img_data = generate_qr_img(str(redirect_url), file_path)

        return StreamingResponse(io.BytesIO(img_data), media_type="image/png")

    return FileResponse(file_path)
