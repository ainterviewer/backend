from typing import Literal

from fastapi import APIRouter
from pydantic import UUID4, BaseModel, model_validator

from ...db.models import InvitationCreate, InvitationPublic
from ...dependencies import AdminToken, DBSession
from ...types import Scope

router = APIRouter(
    prefix="/admin",
)


class AccessRequestsProcessRequest(BaseModel):
    ids: list[UUID4]
    scopes: list[Scope]
    action: Literal["approve", "deny"]

    @model_validator(mode="after")
    def validate_data(self):
        assert len(self.ids) == len(self.scopes)

        return self


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
    for id_, scope in zip(requests.ids, requests.scopes):
        await db.users.process_access_request(
            id_,
            scope=scope,
            action=requests.action,
            approver_id=jwt.user_id,
        )


@router.post("/access-requests/delete")
async def delete_access_requests(
    request: AccessRequestsDeleteRequest,
    db: DBSession,
    jwt: AdminToken,
):
    db.users.delete_access_requests(request.ids)


@router.get("/invitations")
async def get_invitations(
    db: DBSession,
    jwt: AdminToken,
) -> list[InvitationPublic]:
    return db.users.get_invitations()


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
