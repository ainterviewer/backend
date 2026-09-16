from fastapi import APIRouter

from ...response_models import ErrorResponse
from . import codes, comments, embeddings, monitoring, report

router = APIRouter(
    responses={400: {"description": "Invalid request", "model": ErrorResponse}},
    tags=["analysis"],
)

router.include_router(codes.router)
router.include_router(comments.router)
router.include_router(embeddings.router)
router.include_router(monitoring.router)
router.include_router(report.router)
