from fastapi import APIRouter

from . import access
from . import aws
from . import users

router = APIRouter(prefix="/admin", tags=["admin"])

router.include_router(access.router)
router.include_router(aws.router)
router.include_router(users.router)
