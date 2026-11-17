from fastapi import APIRouter, HTTPException

from ainterviewer.constants import LANGUAGES
from ainterviewer.settings import settings as lib_settings
from ainterviewer.types import LanguageDict

from ..auth import Scope
from ..dependencies import DBSession, DemoToken
from ..distribution import speech_is_test_only
from ..schemas.common import InstanceInfo, ModelOption
from ..schemas.users import NewsletterRequest
from ..settings import app_settings

router = APIRouter()


@router.get("/instance", description="Contact and links for this deployment")
def get_instance() -> InstanceInfo:
    return InstanceInfo.model_validate(
        {**app_settings.app.model_dump(), "speech_test_only": speech_is_test_only()}
    )


@router.get("/models", description="available models")
def get_models(jwt: DemoToken) -> list[ModelOption]:
    llm = lib_settings.llm
    # Never None once settings validate; `finalize` fills it in.
    models = (
        (llm.demo_models or []) if jwt.scope == Scope.DEMO else llm.available_models
    )
    return [
        ModelOption(id=model, test_only=llm.is_test_only(model)) for model in models
    ]


@router.get("/languages", description="Get supported languages")
async def get_languages(jwt: DemoToken) -> list[LanguageDict]:
    return LANGUAGES


@router.post("/newsletter", description="Subscribe to our newsletter")
async def newsletter_subscribe(payload: NewsletterRequest, db: DBSession):
    db.newsletter.subscribe(payload.email)


@router.delete("/newsletter", description="Unsubscribe from our newsletter")
async def newsletter_unsubscribe(payload: NewsletterRequest, db: DBSession):
    if not db.newsletter.unsubscribe(payload.email):
        raise HTTPException(404, "Email not subscribed")
