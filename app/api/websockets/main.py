from fastapi import APIRouter

from .interviews import ai

router = APIRouter()

router.include_router(ai.router)
