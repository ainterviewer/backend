from fastapi import APIRouter

from ainterviewer.constants import LANGUAGES
from ainterviewer.settings import settings as lib_settings
from ainterviewer.types import LanguageDict

from ..auth import Scope
from ..dependencies import DemoToken

router = APIRouter()


@router.get("/models", description="available models")
def get_models(jwt: DemoToken):
    if jwt.scope == Scope.DEMO:
        return lib_settings.llm.demo_models
    else:
        return lib_settings.llm.available_models


@router.get("/languages", description="Get supported languages")
async def get_languages(jwt: DemoToken) -> list[LanguageDict]:
    return LANGUAGES
