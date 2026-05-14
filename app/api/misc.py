from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr

from ainterviewer.constants import LANGUAGES
from ainterviewer.settings import settings as lib_settings
from ainterviewer.types import LanguageDict

from ..auth import Scope
from ..dependencies import DBSession, DemoToken

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


class NewsletterRequest(BaseModel):
    email: EmailStr


@router.post("/newsletter", description="Subscribe to our newsletter")
async def newsletter_subscribe(payload: NewsletterRequest, db: DBSession):
    db.newsletter.subscribe(payload.email)


@router.delete("/newsletter", description="Unsubscribe from our newsletter")
async def newsletter_unsubscribe(payload: NewsletterRequest, db: DBSession):
    if not db.newsletter.unsubscribe(payload.email):
        raise HTTPException(404, "Email not subscribed")
