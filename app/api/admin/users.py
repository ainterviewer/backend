from fastapi import APIRouter
from pydantic import UUID4

from ...dependencies import AdminToken, DBSession

router = APIRouter()


@router.get("/users")
async def get_users(db: DBSession, jwt: AdminToken):
    return db.users.get_users()


@router.delete("/users")
async def delete_user(user_id: UUID4, db: DBSession, jwt: AdminToken):
    return db.users.delete_user(id=user_id)
