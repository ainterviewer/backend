import copy
import json
import os
import random
from pathlib import Path
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Cookie, Header, HTTPException, Query, Request
from fastapi import Path as URLPath
from fastapi.responses import RedirectResponse
from jose.exceptions import ExpiredSignatureError, JWTError
from pydantic import UUID4
from sqlalchemy.exc import NoResultFound
from uvicorn.config import logger

from ainterviewer.constants import LANGUAGES
from ainterviewer.types import LanguageCode, TestType

from ..auth import decode_interview_token, decode_jwt
from ..dependencies import (
    DBSession,
    LocalizationCookie,
    UserToken,
    templates,
    templates_dir,
)
from ..settings import app_settings
from ..translations import MODALS
from ..types import InterviewType, ProjectStatus, Scope
from ..utils import parse_url_query_params, replay_history

router = APIRouter()


@router.get("/interview/redirect")
async def redirect_interview(
    request: Request,
    db: DBSession,
    experiment_id: Optional[UUID4] = Query(None, alias="id"),
):
    if experiment_id is None:
        return templates.TemplateResponse(
            name="missing.html",
            context={
                "request": request,
                "error_sub_heading": "Experiment ID is missing from the URL",
            },
            status_code=404,
        )

    experiment = db.get_experiment(experiment_id)

    project_id = random.choices(experiment.project_ids, weights=experiment.weights)[0]

    return RedirectResponse(f"/interview?id={str(project_id)}&x={experiment_id}")


@router.get("/interview")
@router.get("/interview/{interview_type}")
async def interview(
    request: Request,
    db: DBSession,
    interview_type: Annotated[InterviewType | str, URLPath()] | None = None,
    test_type: Annotated[TestType | str | None, Query()] = None,
    lang: LanguageCode | None = Query(None),
    project_id: Annotated[UUID4 | None, Query(alias="id")] = None,
    experiment_id: Annotated[UUID4 | None, Query(alias="x")] = None,
    token: Optional[str] = Query(None),
    # TODO: make this a more generic external_id when FastAPI finally (sigh)
    # supports multiple-aliases
    user1: Annotated[
        str | None, Query(alias="i.user1", description="user id provided by Epinion")
    ] = None,
    test: Annotated[bool, Query(description="test flag provided by Epinion")] = False,
    referer: Annotated[str | None, Header()] = None,
):
    if interview_type and interview_type not in InterviewType:
        redirect_url = "/interview"
        if project_id:
            redirect_url += f"&id={project_id}"
        if token:
            redirect_url += f"&token={token}"
        return RedirectResponse(redirect_url)

    if test_type and not interview_type == InterviewType.SYNTHETIC:
        return templates.TemplateResponse(
            name="missing.html",
            context={
                "request": request,
                "error_sub_heading": f"Invalid interview type {interview_type} for test type {test_type}",
            },
            status_code=400,
        )

    if project_id is None:
        # TODO:
        # Check for env var DEFAULT_INTERVIEW_ID first
        return templates.TemplateResponse(
            name="missing.html",
            context={
                "request": request,
                "error_sub_heading": "Interview ID is missing from the URL",
            },
            status_code=404,
        )

    forward_params: dict[str, Any] = {"test": test, "user1": user1}

    create_new_interview: bool = False

    if lang:
        if lang != request.cookies.get("language"):
            create_new_interview = True
        language_switch = False
    else:
        language_switch = True

    # Token handling
    if token is None:
        token = request.cookies.get("interview_token")

    if token:
        try:
            interview_token = decode_interview_token(token)
            if (interview_id := interview_token.interview_id) is None:
                project_id = interview_token.project_id
                create_new_interview = True
            else:
                try:
                    interview = db.get_interview(project_id, interview_id, full=True)
                    if 0 < interview.n_messages < 4:
                        create_new_interview = True
                except NoResultFound:
                    create_new_interview = True

        except (JWTError, ExpiredSignatureError):
            create_new_interview = True
    else:
        create_new_interview = True

    project = db.get_project(project_id)

    if project.status == ProjectStatus.INACTIVE:
        return templates.TemplateResponse(
            request,
            name="site/interview/inactive.html",
        )

    if lang is None:
        lang = project.config.default_language

    project_localization = db.get_project_localization(project_id, language=lang)

    if project.config.with_consent:
        if consent := project_localization.interview_guide.consent:
            consent = consent.model_dump()
            consent = (
                MODALS["consent"].get(lang, MODALS["consent"]["EN"]).copy() | consent
            )
        else:
            consent = MODALS["consent"].get(lang, MODALS["consent"]["EN"]).copy()
    else:
        consent = None

    welcome_modal = MODALS["welcome"].get(lang, MODALS["welcome"]["EN"]).copy()

    if project.config.with_welcome:
        if project_localization.interview_guide.welcome is None:
            welcome = {}
        else:
            welcome = project_localization.interview_guide.welcome.model_dump()

        welcome = dict(
            section_before_id=welcome_modal.pop("section_before_id").format(
                email=welcome.pop("email") if "email" in welcome else None,
            ),
            section_after_id=welcome_modal.pop("section_after_id"),
            **welcome_modal | welcome,
        )
        if consent is None:
            welcome["display_modal"] = True  # type: ignore
    else:
        welcome = None

    languages = (
        project.available_languages
        if language_switch
        and project.available_languages
        and len(project.available_languages) > 1
        else None
    )

    _MODALS = copy.deepcopy(MODALS)

    # Prepare response
    response = templates.TemplateResponse(
        request=request,
        name="site/interview/index.jinja",
        context={
            "lang": lang,
            "project_id": str(project_id),
            "help_text": _MODALS["help"]
            .get(lang, _MODALS["help"]["EN"])
            .pop("help_text")
            .format(
                model=project_localization.agent_configs.probing.model,
                email="hc@sodas.ku.dk",
            ),
            **_MODALS["help"].get(lang, _MODALS["help"]["EN"]),
            **_MODALS["exit"].get(lang, _MODALS["exit"]["EN"]),
            "new_interview": create_new_interview,
            "languages": languages,
            "consent": consent,
            "welcome": welcome,
        },
    )

    if lang:
        response.set_cookie(
            key="language",
            value=lang,
            secure=True,
            samesite="none",
        )

    response.set_cookie(
        key="forward_params",
        value=json.dumps(forward_params),
        secure=True,
        samesite="none",
    )

    if referer:
        if referer_id_key := project.config.referer_id_key:
            referer_params = parse_url_query_params(referer)
            # NOTE:
            # May raise a key error if the referer does not contain the key
            try:
                referer_id_value = referer_params[referer_id_key][0]
            except KeyError:
                logger.warning(
                    f"Referer ID key '{referer_id_key}' not found in referer URL: {referer_params}"
                )

                return response
        else:
            referer_id_value = referer

        response.set_cookie(
            key="referer",
            value=referer_id_value,
            secure=True,
            samesite="none",
        )

    return response


@router.get("/login")
async def login(
    request: Request,
    invite_token: Annotated[str | None, Query(alias="token")] = None,
):
    token = request.cookies.get("token")

    response = templates.TemplateResponse(
        request=request,
        name="site/login.html",
        context={"token": True if invite_token else False},
    )

    if token is not None:
        try:
            decode_jwt(token)
            return RedirectResponse("/dashboard")
        except Exception:
            response.delete_cookie(
                key="token",
                secure=True,
                httponly=True,
                samesite="none",
            )

    return response


@router.get("/dashboard/projects/prompts")
async def prompts(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="/site/dashboard/projects/prompts.html",
        context={},
    )


@router.get("/dashboard/projects/agents")
async def agents(request: Request):
    return templates.TemplateResponse(
        request=request,
        name=f"/site{request.url.path}.html",
        context={
            "models": app_settings.vllm.available_models,
            "languages": LANGUAGES,
        },
    )


@router.get("/dashboard/projects/config")
async def config(
    request: Request,
    db: DBSession,
    jwt: UserToken,
    project_id: Annotated[UUID4 | None, Cookie()] = None,
):
    if not project_id:
        return RedirectResponse("/dashboard")

    project = db.get_project(
        project_id,
        with_localizations=True,
    )

    return templates.TemplateResponse(
        request=request,
        name=f"/site{request.url.path}.html",
        context={
            "models": app_settings.vllm.available_models,
            "languages": project.available_languages,
            "project_title": project.title,
            "project_status": project.status,
            "project_status_change": "activate"
            if project.status == ProjectStatus.INACTIVE
            else "inactivate",
        },
    )


@router.get("/dashboard/projects/consent")
async def consent(
    request: Request,
    db: DBSession,
    jwt: UserToken,
    language: LocalizationCookie = "EN",
    project_id: Annotated[UUID4 | None, Cookie()] = None,
):
    if not project_id:
        return RedirectResponse("/dashboard")

    project_localization = db.get_project_localization(
        project_id,
        language,
    )

    if project_localization.interview_guide and (
        consent := project_localization.interview_guide.consent
    ):
        context = consent.model_dump()
    else:
        context = {}

    return templates.TemplateResponse(
        request=request,
        name=f"/site{request.url.path}.html",
        context=context,
    )


@router.get("/dashboard/projects/welcome")
async def welcome(
    request: Request,
    db: DBSession,
    jwt: UserToken,
    language: LocalizationCookie,
    project_id: Annotated[UUID4 | None, Cookie()] = None,
):
    if not project_id:
        return RedirectResponse("/dashboard")

    project_localization = db.get_project_localization(
        project_id,
        language,
    )

    if project_localization.interview_guide and (
        welcome := project_localization.interview_guide.welcome
    ):
        context = welcome.model_dump()
    else:
        context = {}

    return templates.TemplateResponse(
        request=request,
        name=f"/site{request.url.path}.html",
        context=context,
    )


@router.get("/dashboard/projects/interviews/{interview_id}")
async def view_interview(
    request: Request,
    db: DBSession,
    jwt: UserToken,
    interview_id: Annotated[UUID4, URLPath],
    localization: LocalizationCookie = "EN",
    project_id: Annotated[UUID4 | None, Cookie()] = None,
):
    if not project_id:
        return RedirectResponse("/dashboard")

    interview_history = db.get_messages(interview_id, project_id)

    messages, _ = replay_history(interview_history, interview_id)

    return templates.TemplateResponse(
        request=request,
        name="/site/dashboard/projects/interviews/view.html",
        context={"messages": messages},
    )


@router.get("/dashboard/projects/tests/setup")
async def setup_fixed(
    request: Request,
    db: DBSession,
    jwt: UserToken,
    localization: LocalizationCookie = "EN",
    project_id: Annotated[UUID4 | None, Cookie()] = None,
    test_id: Annotated[UUID4 | None, Cookie()] = None,
    test_type: Annotated[TestType | None, Cookie()] = None,
):
    if not project_id:
        return RedirectResponse("/dashboard")

    if not test_type or not test_id:
        return RedirectResponse("/dashboard/projects/tests")

    project = db.get_project(
        project_id,
        with_tests=True,
        with_localizations=True,
    )

    project_localization = db.get_project_localization(
        project_id,
        localization,
    )

    if test_id not in [test.id for test in project.tests]:
        return RedirectResponse("/dashboard/projects/tests")

    if not project_localization.interview_guide:
        return RedirectResponse("/dashboard/projects/guide")

    if test_type == TestType.FIXED_ANSWERS:
        return templates.TemplateResponse(
            request=request,
            name="/site/dashboard/projects/tests/setup_fixed.html",
            context={
                "questions": [
                    question.main_question
                    for section in project_localization.interview_guide.question_sections
                    for question in section.questions
                    if question.can_answer
                ]
            },
        )
    elif test_type == TestType.SHUFFLED_AI:
        return templates.TemplateResponse(
            request=request,
            name="/site/dashboard/projects/tests/setup_shuffled.html",
        )
    else:
        return RedirectResponse("/dashboard/projects/tests")


@router.get("/dashboard/projects/tests/runs")
async def test_runs(
    request: Request,
    db: DBSession,
    jwt: UserToken,
    project_id: Annotated[UUID4 | None, Cookie()] = None,
    test_id: Annotated[UUID4 | None, Cookie()] = None,
    test_type: Annotated[TestType | None, Cookie()] = None,
):
    if not project_id:
        return RedirectResponse("/dashboard")

    if not test_type or not test_id:
        return RedirectResponse("/dashboard/projects/tests")

    project = db.get_project(
        project_id,
        with_tests=True,
        with_localizations=True,
    )

    if not (test_setups := [test for test in project.tests if test.id == test_id]):
        return RedirectResponse("/dashboard/projects/tests")
    else:
        test_setup = test_setups[0]

    available_language_codes = (
        {loc["code"] for loc in project.available_languages}
        if project.available_languages
        else {}
    )

    return templates.TemplateResponse(
        request=request,
        name=f"/site{request.url.path}.html",
        context={
            "n_interviews": test_setup.n_interviews,
            "delay_before_answers": test_setup.delay_before_answers,
            "models": (
                None
                if test_setup.type == TestType.FIXED_ANSWERS
                else app_settings.vllm.available_models
            ),
            "model": test_setup.answering_model,
            "languages": [
                lang for lang in LANGUAGES if lang["code"] in available_language_codes
            ],
            "language": test_setup.language,
        },
    )


@router.get("/dashboard")
@router.get("/dashboard/{page_name:path}")
async def dashboard(
    request: Request,
    jwt: UserToken,
    page_name: str = "",
):
    if isinstance(jwt, RedirectResponse):
        return jwt

    base_path = Path("site") / "dashboard"

    route, extension = os.path.splitext(page_name)
    if not extension:
        extension = ".html"

    if route == "":
        route = "index"

    full_path = base_path / f"{route}{extension}"

    if not (templates_dir / full_path).exists():
        full_path = base_path / route / "index.html"

    is_admin = jwt.scope == Scope.ADMIN

    return templates.TemplateResponse(
        request=request,
        name=str(full_path),
        context={
            "request": request,
            "page_name": page_name,
            "is_admin": is_admin,
            "languages": LANGUAGES,
        },
    )


@router.get("/{page_name:path}", include_in_schema=False)
async def serve_page(request: Request, page_name: str = ""):
    if page_name.startswith("api/"):
        raise HTTPException(status_code=404, detail="Not Found")

    route = page_name if page_name else "index"
    route, extension = os.path.splitext(route)
    if not extension:
        extension = ".html"

    full_path = f"site/{route}{extension}"

    return templates.TemplateResponse(
        request=request,
        name=full_path,
        context={"request": request, "page_name": page_name},
    )
