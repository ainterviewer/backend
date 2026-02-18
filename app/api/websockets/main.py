from fastapi import APIRouter

from . import interviews

router = APIRouter()

router.include_router(interviews.router)
