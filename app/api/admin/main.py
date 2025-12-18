from fastapi import APIRouter

from . import access
from . import aws

router = APIRouter(prefix="/admin", tags=["admin"])

router.include_router(access.router)
router.include_router(aws.router)
