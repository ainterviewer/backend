import datetime
import io
import json
from base64 import b64decode
from pathlib import Path
from typing import Annotated

import polars as pl
from fastapi import (
    APIRouter,
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
from pydantic import UUID4, EmailStr
from xlsxwriter import Workbook

from ainterviewer.config import AgentConfigs, InterviewConfig
from ainterviewer.interview_guides import InterviewGuide
from ainterviewer.interview_guides.extra import Consent, Welcome
from ainterviewer.interview_guides.generate import (
    generate_interview_guide,
    generate_question,
    generate_section,
)
from ainterviewer.settings import settings as lib_settings
from ainterviewer.types import LanguageCode, LanguageDict

from ...db.models import InterviewSummaryPublic, MessagePublic, ProjectPublic
from ...db.types import InterviewType
from ...db.utils import fix_nested_columns
from ...dependencies import (
    DBSession,
    DemoToken,
    ProjectAdmin,
    ProjectEditor,
    ProjectViewer,
    UserToken,
)
from ...utils import generate_qr_img
from ..request_models import (
    DeleteInterviewRequest,
    ExportMessagesRequest,
    InterviewGuideGenerationRequest,
    PaginatedQueryParams,
    ProjectStatusChangeRequest,
    ProjectTitleUpdateRequest,
    PromptsUpdateRequest,
    QuestionGenerationRequest,
    QuestionSectionGenerationRequest,
)
from ..response_models import PaginatedResponse

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


@router.post(
    "/projects/{project_id}/available_languages",
    description="Set project language",
    response_model=list[LanguageDict],
)
async def add_project_languages(
    project_id: UUID4,
    language: Annotated[LanguageCode, Body()],
    db: DBSession,
    jwt: DemoToken,
    _: ProjectEditor,
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
):
    guide = await generate_interview_guide(data.prompt, output_path=None)

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
):
    project = db.projects.get_project_localization(project_id, lang)

    section = await generate_section(data.prompt, guide=project.interview_guide)

    return section


@router.post("/projects/{project_id}/{lang}/guide/section/question/generate")
async def generate_section_question(
    project_id: UUID4,
    lang: LanguageCode,
    data: QuestionGenerationRequest,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectEditor,
):
    project = db.projects.get_project_localization(project_id, lang)

    question = await generate_question(
        data.prompt,
        guide=project.interview_guide,
        section=project.interview_guide.question_sections[data.section_idx],
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
    filepath: Path = (
        lib_settings.storage.project_storage.image_path(project_id) / file.filename
    )

    with open(filepath, "wb") as f:
        f.write(file.file)


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


@router.get("/projects/{project_id}/config")
async def get_interview_config(
    project_id: UUID4,
    db: DBSession,
) -> InterviewConfig:
    project = db.projects.get_project(project_id)

    return project.config


@router.post("/projects/{project_id}/config")
async def create_interview_config(
    project_id: UUID4,
    config: InterviewConfig,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectEditor,
):
    db.projects.update_interview_config(project_id, config)


@router.get("/projects/{project_id}/{lang}/prompts")
async def get_prompts(
    project_id: UUID4,
    lang: LanguageCode,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectViewer,
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
    jwt: DemoToken,
    _: ProjectEditor,
):
    db.projects.update_prompts(
        project_id=project_id,
        language=lang,
        prompts=prompts,
    )


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

    rows = []

    for message in messages:
        d = json.loads(message.model_dump_json())

        if d.get("feedback") is None:
            d["feedback"] = ""

        if (attachment := d.get("attachment")) is not None:
            d["attachment"] = str(attachment)
        else:
            d["attachment"] = ""

        if (image := d.get("image")) is not None:
            d["image"] = json.dumps(image)
        else:
            d["image"] = ""

        if (survey_item := d.get("survey_item")) is not None:
            d["survey_item"] = json.dumps(survey_item)
        else:
            d["survey_item"] = ""

        if (annotations := d.get("annotations")) is not None:
            d["annotations"] = json.dumps(annotations)
        else:
            d["annotations"] = ""

        rows.append(d)

    df = pl.from_dicts(rows)

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
                            worksheet=interview_id[0][:31],
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
