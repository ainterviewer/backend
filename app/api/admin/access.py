from fastapi import APIRouter
from pydantic import UUID4

from ...dependencies import AdminToken, DBSession
from ...schemas.admin import (
    AccessRequestsDeleteRequest,
    AccessRequestsProcessRequest,
    InvitationsDeleteRequest,
)
from ...schemas.users import (
    AccessRequestPublic,
    InvitationCreate,
    InvitationPublic,
    InvitationUpdate,
)

router = APIRouter(
    prefix="/admin",
)


@router.get("/access-requests")
async def get_access_requests(
    db: DBSession,
    jwt: AdminToken,
) -> list[AccessRequestPublic]:
    return list(db.users.get_access_requests())


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


@router.put("/invitations/{invitation_id}")
async def update_invitation(
    invitation_id: UUID4,
    request: InvitationUpdate,
    db: DBSession,
    jwt: AdminToken,
) -> InvitationPublic:
    return db.users.update_invitation(invitation_id, request)


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
