from fastapi import APIRouter

from . import assistance
from . import interviews

router = APIRouter()

router.include_router(assistance.router)
router.include_router(interviews.router)
