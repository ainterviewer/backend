from fastapi import APIRouter, Request
from pydantic import UUID4

from ...dependencies import DBSession, UserToken

router = APIRouter(tags=["collaborators"])


@router.get("/dashboard/collaborators")
async def get_collaborators(
    request: Request,
    project_id: UUID4,
    db: DBSession,
    jwt: UserToken,
): ...
