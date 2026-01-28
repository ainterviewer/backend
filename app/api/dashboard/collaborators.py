from fastapi import APIRouter, Request
from pydantic import UUID4

from ...db.models import CollaboratorCreate, CollaboratorPublic
from ...dependencies import DBSession, UserToken
from ...types import CollaboratorRole

router = APIRouter(tags=["collaborators"])


@router.get("/dashboard/collaborators", response_model=list[CollaboratorPublic])
async def get_collaborators(
    request: Request, db: DBSession, jwt: UserToken, folder_id: UUID4
):
    return db.projects.get_collaborators(folder_id)


@router.post("/dashboard/collaborators", response_model=CollaboratorPublic)
async def add_collaborator(
    collaborator: CollaboratorCreate,
    db: DBSession,
    jwt: UserToken,
):
    return db.projects.add_collaborator(
        collaborator.folder_id,
        collaborator.email,
        collaborator.role,
        jwt.user_id,
    )


@router.delete("/dashboard/collaborators")
async def remove_collaborator(
    folder_id: UUID4,
    user_id: UUID4,
    db: DBSession,
    jwt: UserToken,
):
    db.projects.remove_collaborator(folder_id, user_id)


@router.patch("/dashboard/collaborators", response_model=CollaboratorPublic)
async def update_collaborator_role(
    folder_id: UUID4,
    user_id: UUID4,
    role: CollaboratorRole,
    db: DBSession,
    jwt: UserToken,
):
    return db.projects.update_collaborator_role(folder_id, user_id, role)
