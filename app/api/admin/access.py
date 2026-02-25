from app.db.models import InvitationCreate, InvitationPublic
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


class AccessRequestsDeleteRequest(BaseModel):
    ids: list[UUID4]


class InvitationsDeleteRequest(BaseModel):
    ids: list[UUID4]


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


@router.post("/access-requests/delete")
async def delete_access_requests(
    request: AccessRequestsDeleteRequest,
    db: DBSession,
    jwt: AdminToken,
):
    db.users.delete_access_requests(request.ids)


@router.get("/invitations")
async def get_reuseable_invitations(
    db: DBSession,
    jwt: AdminToken,
) -> list[InvitationPublic]:
    return db.users.get_reuseable_invitations()


@router.post("/invitations")
async def create_invitation(
    request: InvitationCreate,
    db: DBSession,
    jwt: AdminToken,
):
    db.users.create_invitation(**request.model_dump())


@router.post("/invitations/delete")
async def delete_invitations(
    request: InvitationsDeleteRequest,
    db: DBSession,
    jwt: AdminToken,
):
    db.users.delete_invitations(ids=request.ids)
