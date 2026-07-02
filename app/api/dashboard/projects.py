import datetime
import io
from base64 import b64decode
from pathlib import Path
from typing import Annotated

import aiofiles
from fastapi import (
    APIRouter,
    Body,
    Depends,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from pydantic import UUID4, EmailStr
from sqlalchemy.exc import NoResultFound

from ainterviewer.agents.config import (
    DEFAULT_PROBING_SLOTS,
    AgentConfigs,
    ProbingAgentConfig,
    ProbingPromptSlots,
)
from ainterviewer.agents.prompts.agent_prompts import ProbingAgentPrompts
from ainterviewer.config import InterviewConfig
from ainterviewer.interview_guides import InterviewGuide, Question, QuestionSection
from ainterviewer.interview_guides.extra import Consent, Welcome
from ainterviewer.interview_guides.generate import (
    generate_interview_guide,
    generate_question,
    generate_section,
)
from ainterviewer.settings import settings as lib_settings
from ainterviewer.types import LanguageCode, LanguageDict
from ainterviewer.utils import get_language_dict

from ...db.models import InterviewSummaryPublic, MessagePublic, ProjectPublic
from ...db.types import InterviewType
from ...db.utils import (
    fix_nested_columns,
    messages_to_dataframe,
    write_messages_xlsx,
)
from ...dependencies import (
    DBSession,
    DemoToken,
    ProjectAdmin,
    ProjectEditor,
    ProjectViewer,
)
from ...utils import ensure_filename, generate_qr_img
from ..request_models import (
    DeleteInterviewRequest,
    ExportMessagesRequest,
    ExternalParamsRequest,
    InterviewGuideGenerationRequest,
    PaginatedQueryParams,
    ProjectStatusChangeRequest,
    ProjectTitleUpdateRequest,
    QuestionGenerationRequest,
    QuestionSectionGenerationRequest,
)
from ..response_models import (
    InterviewConfigWithModels,
    PaginatedResponse,
    ProbingPromptPreview,
)

router = APIRouter(tags=["projects"])


@router.delete("/projects/{project_id}")
async def delete_project(
    request: Request,
    project_id: UUID4,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectAdmin,
):
    db.projects.delete_project(project_id)


@router.post("/projects/{project_id}/clone")
async def clone_project(
    request: Request,
    project_id: UUID4,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectEditor,
) -> ProjectPublic:
    return db.projects.clone_project(project_id, owner_id=jwt.user_id)


@router.get(
    "/projects/{project_id}/available_languages",
    description="Adds a new localization to the project",
    response_model=list[LanguageDict],
)
async def get_project_languages(
    project_id: UUID4,
    db: DBSession,
):
    return db.projects.get_available_languages_optimized(project_id)


@router.post(
    "/projects/{project_id}/available_languages",
    description="Adds a new localization to the project",
    response_model=list[LanguageDict],
)
async def add_project_language(
    project_id: UUID4,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectEditor,
    language: Annotated[LanguageCode, Body()],
    translate: Annotated[bool, Body()] = True,
):
    if isinstance(jwt, RedirectResponse):
        return jwt

    available_languages = await db.projects.add_project_language(
        project_id, language, translate
    )

    return available_languages


@router.delete(
    "/projects/{project_id}/available_languages",
    description="Remove project language",
    response_model=list[LanguageDict],
)
async def remove_project_language(
    project_id: UUID4,
    language: Annotated[LanguageCode, Body()],
    db: DBSession,
    jwt: DemoToken,
    _: ProjectEditor,
):
    available_languages = db.projects.remove_project_language(project_id, language)

    return available_languages


@router.patch(
    "/projects/{project_id}/status",
    description="Change project status",
)
async def change_project_status(
    project_id: UUID4,
    status_request: ProjectStatusChangeRequest,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectAdmin,
):
    db.projects.change_project_status(project_id, status_request.status)


@router.patch(
    "/projects/{project_id}/title",
    description="Change project title",
)
async def change_project_title(
    project_id: UUID4,
    title_request: ProjectTitleUpdateRequest,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectAdmin,
):
    db.projects.update_project_title(project_id, title_request.title)


@router.get("/projects/{project_id}", description="Load projects")
async def get_project(
    project_id: UUID4,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectViewer,
) -> ProjectPublic:
    return db.projects.get_project(project_id)


@router.get("/projects/{project_id}/{lang}/guide")
async def get_guide(
    project_id: UUID4,
    lang: LanguageCode,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectViewer,
) -> InterviewGuide:
    project_localization = db.projects.get_project_localization(project_id, lang)
    return project_localization.interview_guide or InterviewGuide()


@router.post("/projects/{project_id}/{lang}/guide")
async def create_guide(
    project_id: UUID4,
    lang: LanguageCode,
    guide: InterviewGuide,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectEditor,
):
    images = [
        question.image
        for section in guide.question_sections
        for question in section.questions
        if question.image
    ]

    for image in images:
        if isinstance(image.data, str) and (image.data.startswith("data:image/")):
            data = b64decode(image.data.split(",")[-1])

            filepath = (
                lib_settings.storage.project_storage.image_path(project_id) / image.name
            )

            with open(filepath, "wb") as f:
                f.write(data)

    db.projects.update_interview_guide(project_id, guide, language=lang)


@router.post("/projects/{project_id}/{lang}/guide/generate")
async def generate_guide(
    project_id: UUID4,
    lang: LanguageCode,
    data: InterviewGuideGenerationRequest,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectEditor,
) -> InterviewGuide:
    language = get_language_dict(language_code=lang)["name"]

    data.prompt += f"\n\nYou must generate the interview guide in the following language: {language}"

    guide = await generate_interview_guide(
        data.prompt,
        model=lib_settings.llm.default_model,
        output_path=None,
    )

    db.projects.update_interview_guide(project_id, guide, language=lang)

    return guide


@router.post("/projects/{project_id}/{lang}/guide/section/generate")
async def generate_guide_section(
    project_id: UUID4,
    lang: LanguageCode,
    data: QuestionSectionGenerationRequest,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectEditor,
) -> QuestionSection:
    project = db.projects.get_project_localization(project_id, lang)

    section = await generate_section(
        data.prompt,
        model=lib_settings.llm.default_model,
        guide=project.interview_guide,
    )

    return section


@router.post("/projects/{project_id}/{lang}/guide/section/question/generate")
async def generate_section_question(
    project_id: UUID4,
    lang: LanguageCode,
    data: QuestionGenerationRequest,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectEditor,
) -> Question:
    project = db.projects.get_project_localization(project_id, lang)

    question = await generate_question(
        data.prompt,
        model=lib_settings.llm.default_model,
        guide=project.interview_guide,
        section=project.interview_guide.question_sections[data.section_idx],
        max_probes_n=data.max_probes_n,
        max_probes_time=data.max_probes_time,
    )

    return question


@router.post("/projects/{project_id}/image")
async def upload_image(
    project_id: UUID4,
    primer: Annotated[str, Form()],
    description: Annotated[str, Form()],
    alt: Annotated[str, Form()],
    file: UploadFile,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectEditor,
):
    filename = ensure_filename(file.filename)

    filepath: Path = (
        lib_settings.storage.project_storage.image_path(project_id) / filename
    )

    async with aiofiles.open(filepath, "wb") as f:
        contents = await file.read()
        await f.write(contents)


@router.get("/projects/{project_id}/{lang}/agents")
async def get_interview_agents(
    project_id: UUID4,
    lang: LanguageCode,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectViewer,
) -> AgentConfigs:
    project_localization = db.projects.get_project_localization(
        project_id,
        language=lang,
    )

    return project_localization.agent_configs


@router.post("/projects/{project_id}/{lang}/agents")
async def create_interview_agents(
    project_id: UUID4,
    lang: LanguageCode,
    config: AgentConfigs,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectEditor,
):
    db.projects.update_agent_configs(
        project_id=project_id,
        language=lang,
        agent_configs=config,
    )


# Labelled stand-ins for the interview-time context the instruction prompt
# expects. They let us render the template at config time so the user can see
# how their edited slots land, without needing a live interview.
_PREVIEW_PLACEHOLDERS = {
    "interview_framing": "«interview framing — filled in during the interview»",
    "section_description": "«section description — filled in during the interview»",
    "question_description": "«question description — filled in during the interview»",
    "main_question": "«main question — filled in during the interview»",
    "interview_transcript": "«transcript so far — filled in during the interview»",
    "suggested_probes": "«suggested probes for this question — included when configured»",
    "translation": "«interview language — included when the interview is not in English»",
}


@router.post("/projects/{project_id}/{lang}/agents/prompts/preview")
async def preview_probing_prompts(
    project_id: UUID4,
    lang: LanguageCode,
    config: ProbingAgentConfig,
    jwt: DemoToken,
    _: ProjectViewer,
) -> ProbingPromptPreview:
    """Render the probing agent's system and instruction prompts with the
    supplied (possibly unsaved) slot overrides injected, for a read-only preview."""
    prompts = ProbingAgentPrompts(prompt_slots=config.prompt_slots, lang=lang)

    instruction = prompts.generate_probing_prompt(
        **_PREVIEW_PLACEHOLDERS,
        few_shot_examples=config.few_shot_examples,
    )

    return ProbingPromptPreview(
        system=prompts.system_prompt,
        instruction=instruction,
    )


# Conversational, respondent-facing agents whose model(s) we surface to
# respondents. Excludes the auxiliary `visual` agent even though it also
# carries a `.model`.
_RESPONDENT_FACING_AGENTS = frozenset(
    {
        "probing",
        "classification",
        "guide",
        "history",
        "answering",
        "reformulation",
        "security",
    }
)


@router.get("/projects/{project_id}/config")
async def get_interview_config(
    project_id: UUID4,
    db: DBSession,
) -> InterviewConfigWithModels:
    project = db.projects.get_project(project_id)

    models: set[str] = set()

    for language in project.available_languages:  # ty: ignore[not-iterable]
        project_localization = db.projects.get_project_localization(
            project_id=project_id, language=language["code"]
        )

        models.update(
            agent_config.model
            for agent, agent_config in project_localization.agent_configs
            if agent in _RESPONDENT_FACING_AGENTS
        )

    return InterviewConfigWithModels(**project.config.model_dump(), models=models)


@router.post("/projects/{project_id}/config")
async def create_interview_config(
    project_id: UUID4,
    config: InterviewConfig,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectEditor,
):
    db.projects.update_interview_config(project_id, config)


@router.patch("/projects/{project_id}/external_params")
async def update_external_params(
    project_id: UUID4,
    request: ExternalParamsRequest,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectEditor,
):
    db.projects.update_external_params(project_id, request.params)


@router.get("/prompt-defaults")
async def get_prompt_defaults(jwt: DemoToken) -> ProbingPromptSlots:
    """Default values for the editable probing-agent prompt slots.

    Project-specific overrides live on the probing agent config
    (``AgentConfigs.probing.prompt_slots``) and are read/written through the
    ``/agents`` endpoints. The frontend uses these defaults to show the effective
    text for any slot a project has not overridden.
    """
    return DEFAULT_PROBING_SLOTS


# NOTE: Doesn't require auth since it's not sensitive and has to be read
# from the interview page by visitors
@router.get("/projects/{project_id}/guide/consent/{language}")
async def get_consent(
    project_id: UUID4,
    db: DBSession,
    language: LanguageCode,
) -> Consent | None:
    return db.projects.get_consent(
        project_id,
        language=language,
    )


@router.post("/projects/{project_id}/guide/consent/{language}")
async def create_consent(
    project_id: UUID4,
    language: LanguageCode,
    consent: Consent,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectEditor,
):
    db.projects.update_consent(project_id, consent, language)


# NOTE: Doesn't require auth since it's not sensitive and has to be read
# from the interview page by visitors
@router.get("/projects/{project_id}/guide/welcome/{language}")
async def get_welcome(
    project_id: UUID4,
    language: LanguageCode,
    db: DBSession,
) -> Welcome | None:
    return db.projects.get_welcome(
        project_id=project_id,
        language=language,
    )


@router.post("/projects/{project_id}/guide/welcome/{language}")
async def create_welcome(
    project_id: UUID4,
    language: LanguageCode,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectEditor,
    title: Annotated[str, Form()],
    text: Annotated[str, Form()],
    email: Annotated[EmailStr, Form()],
    video: UploadFile | None = None,
):
    welcome = Welcome(title=title, text=text, email=email)

    if video:
        data = await video.read()

        if not video.filename:
            raise ValueError()

        with open(
            lib_settings.storage.project_storage.video_path(project_id)
            / video.filename,
            "wb",
        ) as f:
            f.write(data)

        welcome.video_file_name = video.filename

    db.projects.update_welcome(project_id, welcome, language)


@router.get("/projects/{project_id}/interviews")
async def get_interviews(
    project_id: UUID4,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectViewer,
    paginated_query: Annotated[PaginatedQueryParams, Depends(PaginatedQueryParams)],
    interview_types: Annotated[list[InterviewType] | None, Query()] = None,
    created_at: Annotated[datetime.datetime | None, Query] = None,
    completed: Annotated[bool | None, Query] = None,
) -> PaginatedResponse[InterviewSummaryPublic]:
    interviews, total = db.interviews.get_interviews(
        project_id,
        with_messages=True,
        offset=paginated_query.offset,
        limit=paginated_query.limit,
        sorting_column=paginated_query.column,
        sorting_order=paginated_query.order,
        interview_types=interview_types
        if interview_types is not None
        else [InterviewType.DISTRIBUTED],
        created_at=created_at,
        completed=completed,
    )
    response = [
        InterviewSummaryPublic(**interview.model_dump()) for interview in interviews
    ]

    return PaginatedResponse(total=total, items=response)


@router.delete("/projects/{project_id}/interviews")
async def delete_interviews(
    delete_request: DeleteInterviewRequest,
    project_id: UUID4,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectAdmin,
):
    db.interviews.delete_interviews(project_id, delete_request.interview_ids)


@router.get("/projects/{project_id}/interviews/{interview_id}/messages/{message_id}")
async def get_message(
    project_id: UUID4,
    interview_id: UUID4,
    message_id: UUID4,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectViewer,
):
    # TODO: Implement fetching singular messages based on some id, and maybe
    # before=N and after=N messages from query arg. return
    # db.interviews.get_message(interview_id, project_id)
    ...


@router.get("/projects/{project_id}/interviews/{interview_id}/messages")
async def get_interview_messages(
    request: Request,
    project_id: UUID4,
    interview_id: UUID4,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectViewer,
) -> list[MessagePublic]:
    return db.interviews.get_messages(interview_id, project_id)


@router.get("/projects/{project_id}/interviews/{interview_id}/audio/{filename}")
async def get_interview_audio(
    project_id: UUID4,
    interview_id: UUID4,
    filename: str,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectViewer,
) -> FileResponse:
    """Serve a voice-message recording, referenced by a message's audio_file."""
    try:
        # The audio directory is keyed by interview only; this confirms the
        # interview belongs to the project the caller has access to.
        db.interviews.get_interview(project_id=project_id, interview_id=interview_id)
    except NoResultFound:
        raise HTTPException(status_code=404, detail="Interview not found")

    audio_path = (
        lib_settings.storage.interview_storage.audio_path(interview_id)
        / Path(filename).name  # strips any path components
    )
    if not audio_path.is_file():
        raise HTTPException(status_code=404, detail="Recording not found")

    return FileResponse(audio_path, media_type="audio/wav")


@router.post("/projects/{project_id}/interviews/messages/export")
async def export_messages(
    export_request: ExportMessagesRequest,
    project_id: UUID4,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectViewer,
):
    messages: list[MessagePublic] = [
        message
        for interview in (
            db.interviews.get_messages(interview_id, project_id)
            for interview_id in export_request.interview_ids
        )
        for message in interview
    ]

    df = messages_to_dataframe(messages)

    with io.BytesIO() as stream:
        match export_request.format:
            case "csv":
                df = fix_nested_columns(df)
                df.write_csv(stream)

            case "xlsx":
                write_messages_xlsx(df, stream)

            case _:
                raise ValueError(f"Unsupported format: {format}")

        response = StreamingResponse(
            iter([stream.getvalue()]), media_type=f"text/{format}"
        )
        response.headers["Content-Disposition"] = (
            f"attachment; filename={project_id}-messages.{format}"
        )

        return response


@router.get("/projects/{project_id}/qr.png")
async def generate_project_qr(
    request: Request,
    project_id: UUID4,
    jwt: DemoToken,
    _: ProjectViewer,
):
    file_path = (
        lib_settings.storage.project_storage.qr_code_path(project_id) / "interview.png"
    )

    if not file_path.exists():
        interview_url = str(request.base_url) + f"interview?id={project_id}"
        img_data = generate_qr_img(str(interview_url), file_path)
        # TODO: Why not just return FileResponse after file has been created?
        return StreamingResponse(io.BytesIO(img_data), media_type="image/png")

    return FileResponse(file_path)


@router.post("/projects/{project_id}/owner")
async def check_project_owner(
    project_id: UUID4,
    db: DBSession,
) -> bool:
    return db.projects.is_project_owner_demo_user(project_id)
