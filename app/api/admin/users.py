from fastapi import APIRouter, HTTPException
from pydantic import UUID4, BaseModel

from ...db.models import UNSET, UserAdmin, UserAdminUpdate
from ...dependencies import AdminToken, DBSession
from ...settings import app_settings
from ..dashboard.synthesize import get_demo_budget
from ..response_models import DemoBudget, DemoLimitDefaults
from ..websockets.manager import session_manager

router = APIRouter()


class AdminNoteUpdate(BaseModel):
    note: str | None = None


@router.get("/users")
async def get_users(db: DBSession, jwt: AdminToken) -> list[UserAdmin]:
    return db.users.get_users_admin()


@router.delete("/users")
async def delete_user(user_id: UUID4, db: DBSession, jwt: AdminToken):
    await session_manager.end(db.projects.get_owned_project_ids(user_id))
    return db.users.delete_user(id=user_id)


@router.patch("/users/{user_id}/note")
async def update_admin_note(
    user_id: UUID4, body: AdminNoteUpdate, db: DBSession, jwt: AdminToken
) -> UserAdmin:
    return db.users.update_admin_note(user_id, body.note)


@router.patch("/users/{user_id}")
async def update_user(
    user_id: UUID4, body: UserAdminUpdate, db: DBSession, jwt: AdminToken
) -> UserAdmin:
    if user_id == jwt.user_id and jwt.scope != body.scope and body.scope is not UNSET:
        raise HTTPException(
            status_code=400, detail="Admins cannot change their own scope"
        )
    return db.users.update_user_admin(user_id, body)


@router.get("/demo-limits")
async def get_demo_limit_defaults(jwt: AdminToken) -> DemoLimitDefaults:
    """The configured demo budgets, which a NULL `demo_*_limit` falls back to."""
    limits = app_settings.app.demo_limits
    return DemoLimitDefaults(
        concurrent=limits.max_concurrent_synthetic_interviews,
        daily=limits.daily_synthetic_interviews,
        lifetime=limits.lifetime_synthetic_interviews,
    )


@router.get("/users/{user_id}/demo-budget")
async def get_user_demo_budget(
    user_id: UUID4, db: DBSession, jwt: AdminToken
) -> DemoBudget:
    """A user's demo budgets, overrides applied, and how much of each is spent.

    Answers for any scope, so an admin can see the usage before moving a user
    to DEMO; only DEMO accounts are held to it.
    """
    return get_demo_budget(db, user_id)
