from typing import Literal

from fastapi import APIRouter
from pydantic import UUID4, BaseModel

from ...dependencies import AdminToken, DBSession

router = APIRouter(
    prefix="/admin",
)


class AccessRequestsProcessRequest(BaseModel):
    ids: list[UUID4]
    action: Literal["approve", "deny"]


@router.get("/access-requests")
async def get_access_requests(
    db: DBSession,
    jwt: AdminToken,
):
    access_requests = db.users.get_access_requests()
    return access_requests


@router.post("/access-requests/process")
async def process_access_requests(
    db: DBSession,
    jwt: AdminToken,
    requests: AccessRequestsProcessRequest,
):
    for id_ in requests.ids:
        await db.users.process_access_request(
            id_, requests.action, approver_id=jwt.user_id
        )
