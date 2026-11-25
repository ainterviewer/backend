from fastapi import APIRouter

from . import (
    analysis,
    assistance,
    consent_attachments,
    experiments,
    folders,
    guide_images,
    participants,
    projects,
    reports,
    synthesize,
    versions,
)

router = APIRouter()

router.include_router(analysis.router)
router.include_router(assistance.router)
router.include_router(consent_attachments.router)
router.include_router(guide_images.router)
router.include_router(experiments.router)
router.include_router(folders.router)
router.include_router(participants.router)
router.include_router(participants.public_router)
router.include_router(projects.router)
router.include_router(reports.router)
router.include_router(synthesize.router)
router.include_router(versions.router)
