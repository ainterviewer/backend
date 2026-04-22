from fastapi import APIRouter

from . import access, aws, users

router = APIRouter(prefix="/admin", tags=["admin"])

router.include_router(access.router)
router.include_router(aws.router)
router.include_router(users.router)
