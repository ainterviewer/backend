from fastapi import APIRouter
from pydantic import UUID4, BaseModel

from ...db.models import UserAdmin
from ...dependencies import AdminToken, DBSession

router = APIRouter()


class AdminNoteUpdate(BaseModel):
    note: str | None = None


@router.get("/users")
async def get_users(db: DBSession, jwt: AdminToken) -> list[UserAdmin]:
    return db.users.get_users_admin()


@router.delete("/users")
async def delete_user(user_id: UUID4, db: DBSession, jwt: AdminToken):
    return db.users.delete_user(id=user_id)


@router.patch("/users/{user_id}/note")
async def update_admin_note(
    user_id: UUID4, body: AdminNoteUpdate, db: DBSession, jwt: AdminToken
) -> UserAdmin:
    return db.users.update_admin_note(user_id, body.note)
