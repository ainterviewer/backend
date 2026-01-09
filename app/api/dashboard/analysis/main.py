from fastapi import APIRouter

from ...response_models import ErrorResponse
from . import annotations, embeddings

router = APIRouter(
    responses={400: {"description": "Invalid request", "model": ErrorResponse}},
    tags=["analysis"],
)

router.include_router(annotations.router)
router.include_router(embeddings.router)
