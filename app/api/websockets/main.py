from fastapi import APIRouter

from . import guide
from .interviews import ai, transcription

router = APIRouter()

router.include_router(ai.router)
router.include_router(transcription.router)
router.include_router(guide.router)
