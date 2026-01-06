from fastapi import APIRouter

from ainterviewer.constants import LANGUAGES
from ainterviewer.settings import settings as lib_settings
from ainterviewer.types import LanguageDict

from ..dependencies import UserToken

router = APIRouter()


@router.get("/models", description="available models")
def get_models(jwt: UserToken):
    return lib_settings.llm.available_models


@router.get("/languages", description="Get supported languages")
async def get_languages(jwt: UserToken) -> list[LanguageDict]:
    return LANGUAGES
