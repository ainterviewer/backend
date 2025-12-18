from fastapi import APIRouter

from ainterviewer.constants import LANGUAGES
from ainterviewer.types import LanguageDict

from ..dependencies import UserToken
from ..settings import app_settings

router = APIRouter()


@router.get("/models", description="available models")
def get_models(jwt: UserToken):
    return app_settings.vllm.available_models


@router.get("/languages", description="Get supported languages")
async def get_languages(jwt: UserToken) -> list[LanguageDict]:
    return LANGUAGES
