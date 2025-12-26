import datetime
import io
import json
from base64 import b64decode
from typing import Annotated, Literal

import polars as pl
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Depends,
    Form,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import (
    FileResponse,
    RedirectResponse,
    StreamingResponse,
)
from pydantic import UUID4, BaseModel, EmailStr
from xlsxwriter import Workbook

from ainterviewer.config import AgentConfigs, InterviewConfig
from ainterviewer.constants import FP_ASSETS_DIR
from ainterviewer.interview_guides import InterviewGuide
from ainterviewer.interview_guides.extra import Consent, Welcome
from ainterviewer.interview_guides.generate import generate_interview_guide
from ainterviewer.types import LanguageCode, LanguageDict

from ...db.models import InterviewSummaryPublic, MessagePublic, ProjectPublic
from ...db.utils import fix_nested_columns
from ...dependencies import DBSession, UserToken
from ...paths import QR_CODES_DIR, VIDEO_DIR
from ...utils import generate_qr_img
from ..request_models import (
    CreateProjectRequest,
    InterviewGuideGenerationPromptRequest,
    PaginatedQueryParams,
    ProjectStatusChangeRequest,
    ProjectTitleUpdateRequest,
    PromptsUpdateRequest,
)
from ..response_models import PaginatedResponse

router = APIRouter(tags=["projects"])


@router.get("/projects", description="Load projects")
async def get_projects(
    db: DBSession,
    jwt: UserToken,
) -> list[ProjectPublic]:
    projects = db.projects.get_projects(
        include_available_languages=True,
        include_interview_count=True,
    )

    return projects


@router.get("/projects/ids")
async def get_project_ids(
    db: DBSession,
    jwt: UserToken,
) -> list[UUID4]:
    projects = db.projects.get_projects()

    project_ids = [project.id for project in projects]

    return project_ids


@router.post("/projects", description="Create a new project")
async def create_project(
    request: Request,
    project_request: CreateProjectRequest,
    background_tasks: BackgroundTasks,
    db: DBSession,
    jwt: UserToken,
):
    if isinstance(jwt, RedirectResponse):
        return jwt

    project_id = db.projects.create_project(
        folder_id=project_request.folder_id,
        title=project_request.title,
        interview_config=InterviewConfig(
            default_language=project_request.default_language
        ),
    )

    file_path = QR_CODES_DIR / f"{project_id}.png"
    interview_url = str(request.base_url) + f"interview?id={project_id}"

    background_tasks.add_task(generate_qr_img, str(interview_url), file_path)

    return {"project_id": project_id}


@router.delete("/projects/{project_id}")
async def delete_project(
    request: Request,
    project_id: UUID4,
    db: DBSession,
    jwt: UserToken,
):
    db.projects.delete_project(project_id)


@router.post("/projects/{project_id}/clone")
async def clone_project(
    request: Request,
    project_id: UUID4,
    db: DBSession,
    jwt: UserToken,
) -> ProjectPublic:
    return db.projects.clone_project(project_id)


@router.post(
    "/projects/{project_id}/available_languages",
    description="Set project language",
    response_model=list[LanguageDict],
)
async def add_project_languages(
    project_id: UUID4,
    language: Annotated[LanguageCode, Body()],
    db: DBSession,
    jwt: UserToken,
):
    if isinstance(jwt, RedirectResponse):
        return jwt

    available_languages = db.projects.add_project_language(project_id, language)

    return available_languages


@router.delete(
    "/projects/{project_id}/available_languages",
    description="Remove project language",
    response_model=list[LanguageDict],
)
async def remove_project_languages(
    project_id: UUID4,
    language: Annotated[LanguageCode, Body()],
    db: DBSession,
    jwt: UserToken,
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
    jwt: UserToken,
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
    jwt: UserToken,
):
    db.projects.update_project_title(project_id, title_request.title)


@router.get("/projects/{project_id}", description="Load projects")
async def get_project(
    project_id: UUID4,
    db: DBSession,
    jwt: UserToken,
) -> ProjectPublic:
    return db.projects.get_project(project_id)


@router.get("/projects/{project_id}/{lang}/guide")
async def get_guide(
    project_id: UUID4,
    lang: LanguageCode,
    db: DBSession,
    jwt: UserToken,
) -> InterviewGuide:
    project_localization = db.projects.get_project_localization(project_id, lang)
    return project_localization.interview_guide or InterviewGuide()


@router.post("/projects/{project_id}/{lang}/guide")
async def create_guide(
    project_id: UUID4,
    lang: LanguageCode,
    guide: InterviewGuide,
    db: DBSession,
    jwt: UserToken,
):
    # TODO:
    # - Save images to AWS or other bucket like storage

    images = [
        question.image
        for section in guide.question_sections
        for question in section.questions
        if question.image
    ]

    for image in images:
        if isinstance(image.data, str) and (image.data.startswith("data:image/")):
            data = b64decode(image.data.split(",")[-1])

            with open(FP_ASSETS_DIR / "images" / image.name, "wb") as f:
                f.write(data)

    db.projects.update_interview_guide(project_id, guide, language=lang)


@router.post("/projects/{project_id}/{lang}/guide/generate")
async def generate_guide(
    project_id: UUID4,
    lang: LanguageCode,
    data: InterviewGuideGenerationPromptRequest,
    db: DBSession,
    jwt: UserToken,
):
    guide = await generate_interview_guide(data.prompt, output_path=None)

    db.projects.update_interview_guide(project_id, guide, language=lang)

    return guide


@router.post("/projects/{project_id}/image")
async def upload_image(
    project_id: UUID4,
    primer: Annotated[str, Form()],
    description: Annotated[str, Form()],
    alt: Annotated[str, Form()],
    file: UploadFile,
    db: DBSession,
    jwt: UserToken,
):
    # FIXME: Is the file saved?
    return {
        "primer": primer,
        "description": description,
        "alt": alt,
        "path": file.filename,
    }


@router.get("/projects/{project_id}/{lang}/agents")
async def get_interview_agents(
    project_id: UUID4,
    lang: LanguageCode,
    db: DBSession,
    jwt: UserToken,
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
    jwt: UserToken,
):
    db.projects.update_agent_configs(
        project_id=project_id,
        language=lang,
        agent_configs=config,
    )


@router.get("/projects/{project_id}/config")
async def get_interview_config(
    project_id: UUID4,
    db: DBSession,
    jwt: UserToken,
) -> InterviewConfig:
    project = db.projects.get_project(project_id)

    return project.config


@router.post("/projects/{project_id}/config")
async def create_interview_config(
    project_id: UUID4,
    config: InterviewConfig,
    db: DBSession,
    jwt: UserToken,
):
    db.projects.update_interview_config(project_id, config)


@router.get("/projects/{project_id}/{lang}/prompts")
async def get_prompts(
    project_id: UUID4,
    lang: LanguageCode,
    db: DBSession,
    jwt: UserToken,
):
    project_localization = db.projects.get_project_localization(
        project_id=project_id,
        language=lang,
    )

    return project_localization.prompts


@router.post("/projects/{project_id}/{lang}/prompts")
async def create_prompts(
    project_id: UUID4,
    lang: LanguageCode,
    prompts: PromptsUpdateRequest,
    db: DBSession,
    jwt: UserToken,
):
    db.projects.update_prompts(
        project_id=project_id,
        language=lang,
        prompts=prompts,
    )


@router.get("/projects/{project_id}/guide/consent/{language}")
async def get_consent(
    project_id: UUID4,
    db: DBSession,
    jwt: UserToken,
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
    jwt: UserToken,
):
    db.projects.update_consent(project_id, consent, language)


@router.get("/projects/{project_id}/guide/welcome/{language}")
async def get_welcome(
    project_id: UUID4,
    language: LanguageCode,
    db: DBSession,
    jwt: UserToken,
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
    jwt: UserToken,
    title: Annotated[str, Form()],
    text: Annotated[str, Form()],
    email: Annotated[EmailStr, Form()],
    video: UploadFile | None = None,
):
    welcome = Welcome(title=title, text=text, email=email)

    if video:
        data = await video.read()

        with open(VIDEO_DIR / video.filename, "wb") as f:
            f.write(data)

        welcome.video_file_name = video.filename

    db.projects.update_welcome(project_id, welcome, language)


# https://claude.ai/chat/a9ff8ed8-eb69-4a49-a0e4-c98aef6af66e
@router.get("/projects/{project_id}/interviews")
async def get_interviews(
    project_id: UUID4,
    db: DBSession,
    jwt: UserToken,
    paginated_query: Annotated[PaginatedQueryParams, Depends(PaginatedQueryParams)],
    synthetic: Annotated[bool | None, Query()] = None,
    test: Annotated[bool | None, Query()] = None,
    created_at: Annotated[datetime.datetime | None, Query] = None,
    completed: Annotated[bool | None, Query] = None,
):
    interviews, total = db.interviews.get_interviews(
        project_id,
        with_messages=True,
        offset=paginated_query.offset,
        limit=paginated_query.limit,
        sorting_column=paginated_query.column,
        sorting_order=paginated_query.order,
        synthetic=synthetic,
        test=test,
        created_at=created_at,
        completed=completed,
    )
    response = [
        InterviewSummaryPublic(**interview.model_dump()) for interview in interviews
    ]

    return PaginatedResponse(total=total, items=response)


class DeleteInterviewRequest(BaseModel):
    interview_ids: list[UUID4]


@router.delete("/projects/{project_id}/interviews")
async def delete_interviews(
    delete_request: DeleteInterviewRequest,
    project_id: UUID4,
    db: DBSession,
    jwt: UserToken,
):
    db.interviews.delete_interviews(project_id, delete_request.interview_ids)


@router.get("/projects/{project_id}/interviews/{interview_id}/messages")
async def get_message(
    project_id: UUID4,
    interview_id: UUID4,
    db: DBSession,
    jwt: UserToken,
):
    # TODO: Implement fetching singular messages based on some id, and maybe
    # before=N and after=N messages from query arg. return
    # db.interviews.get_message(interview_id, project_id)
    ...


@router.get("/projects/{project_id}/interviews/{interview_id}/messages")
async def get_messages(
    project_id: UUID4,
    interview_id: UUID4,
    db: DBSession,
    jwt: UserToken,
) -> list[MessagePublic]:
    return db.interviews.get_messages(interview_id, project_id)


class ExportMessagesRequest(BaseModel):
    interview_ids: list[UUID4]
    format: Literal["csv", "xlsx"] = "csv"


@router.get("/projects/{project_id}/interviews/{interview_id}/messages")
async def export_messages(
    export_request: ExportMessagesRequest,
    project_id: UUID4,
    interview_id: UUID4,
    db: DBSession,
    jwt: UserToken,
):
    messages = [
        message
        for interview in (
            db.interviews.get_messages(interview_id, project_id)
            for interview_id in export_request.interview_ids
        )
        for message in interview
    ]

    df = pl.from_dicts([json.loads(message.model_dump_json()) for message in messages])

    with io.BytesIO() as stream:
        match export_request.format:
            case "csv":
                df = fix_nested_columns(df)
                df.write_csv(stream)
            case "xlsx":
                with Workbook(stream) as workbook:
                    text_format = workbook.add_format({"text_wrap": True})
                    for interview_id, interview in df.group_by(
                        "interview_id", maintain_order=True
                    ):
                        interview.write_excel(
                            workbook,
                            column_formats={"backend_content": text_format},
                            column_widths={"backend_content": 400},
                            worksheet=interview_id[0][:31],  # type: ignore
                        )

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
    jwt: UserToken,
):
    file_path = QR_CODES_DIR / "projects" / f"{project_id}.png"
    if not file_path.exists():
        interview_url = str(request.base_url) + f"interview?id={project_id}"
        img_data = generate_qr_img(str(interview_url), file_path)
        # TODO: Why not just return FileResponse after file has been created?
        return StreamingResponse(io.BytesIO(img_data), media_type="image/png")
    return FileResponse(file_path)
