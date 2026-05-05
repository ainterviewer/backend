from fastapi import APIRouter

from . import (
    analysis,
    assistance,
    experiments,
    folders,
    participants,
    projects,
    synthesize,
)

router = APIRouter()

router.include_router(analysis.router)
router.include_router(assistance.router)
router.include_router(experiments.router)
router.include_router(folders.router)
router.include_router(participants.router)
router.include_router(projects.router)
router.include_router(synthesize.router)
