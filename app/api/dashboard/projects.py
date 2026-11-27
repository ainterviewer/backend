import io
import logging
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import aiofiles
from fastapi import (
    APIRouter,
    Body,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from pydantic import UUID4
from sqlalchemy.exc import IntegrityError, NoResultFound

from ainterviewer.agents.config import (
    DEFAULT_PROBING_SLOTS,
    AgentConfigs,
    ProbingAgentConfig,
    ProbingPromptSlots,
)
from ainterviewer.agents.prompts.agent_prompts import ProbingAgentPrompts
from ainterviewer.agents.security_policy import SecurityPolicy, default_security_policy
from ainterviewer.config import InterviewConfig
from ainterviewer.interview_guides import (
    InterviewGuide,
    LocalizedConsent,
    LocalizedInterviewGuide,
    LocalizedWelcome,
    Question,
    QuestionSection,
)
from ainterviewer.interview_guides.extra import Consent, Welcome
from ainterviewer.interview_guides.generate import (
    generate_interview_guide,
    generate_question,
    generate_section,
)
from ainterviewer.interview_guides.localized import (
    LocalizedQuestion,
    LocalizedQuestionSection,
)
from ainterviewer.settings import settings as lib_settings
from ainterviewer.types import InterviewStatus, LanguageCode
from ainterviewer.utils import now

from ...auth import (
    AuthToken,
    create_test_share_token,
    generate_resume_token,
    hash_token,
)
from ...collab.parts import image_name_errors
from ...collab.rooms import content_rooms
from ...db.repositories.interview import SORTABLE_INTERVIEW_COLUMNS
from ...db.repositories.project import ContentSource
from ...db.tables import InterviewResumeTokenTable
from ...dependencies import (
    DBSession,
    DemoToken,
    IfMatch,
    ProjectAdmin,
    ProjectEditor,
    ProjectViewer,
    RespondentContent,
    UserToken,
    demo_restricted,
    is_demo,
    set_etag,
)
from ...errors import AppError
from ...schemas.common import PageParams
from ...schemas.content import (
    ContentDocPublic,
    InterviewGuideGenerationRequest,
    LivePart,
    ProbingPromptPreview,
    ProjectContentPublic,
    QuestionGenerationRequest,
    QuestionSectionGenerationRequest,
)
from ...schemas.interviews import (
    ExportMessagesRequest,
    FacetCount,
    InterviewFacets,
    InterviewFilterParams,
    InterviewListResponse,
    InterviewResumeLinkPublic,
    InterviewResumeLinkResponse,
    InterviewSelection,
    MessagePublic,
    TestShareLinkResponse,
    TestShareRequest,
)
from ...schemas.projects import (
    ArchiveRequest,
    ExperimentOnlyRequest,
    ExternalParamsReplace,
    MoveProjectRequest,
    ProjectCloneRequest,
    ProjectLanguage,
    ProjectPermissionsPublic,
    ProjectPublic,
    ProjectStatusChangeRequest,
    ProjectTitleUpdateRequest,
)
from ...services import interview_storage, project_storage
from ...services.exports.transcripts import (
    MEDIA_TYPES,
    InterviewExport,
    export_interviews,
)
from ...services.storage_cleanup import StorageToDelete
from ...settings import app_settings
from ...types import (
    CollaboratorRole,
    CompletionRedirects,
    ContentPart,
    ErrorCode,
    InterviewType,
    PreviewFeature,
    Scope,
)
from ...utils import as_aware, ensure_filename, generate_qr_img
from ..errors import STALE_REVISION
from ..files import file_response, safe_filename
from ..websockets.manager import session_manager
from .consent_attachments import prune_consent_attachments

logger = logging.getLogger(__name__)

router = APIRouter(tags=["projects"])


@router.delete("/projects/{project_id}")
async def delete_project(
    request: Request,
    project_id: UUID4,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectAdmin,
):
    files = StorageToDelete.of_projects(db, [project_id])
    await session_manager.end([project_id])
    db.projects.delete_project(project_id)
    files.remove()


@router.post("/projects/{project_id}/clone")
async def clone_project(
    request: Request,
    project_id: UUID4,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectEditor,
    body: ProjectCloneRequest | None = None,
) -> ProjectPublic:
    """Copy the project's content and test setups into a new, unpublished
    project. From the draft, or from `version_id`, one of its versions. The
    copy's history starts afresh: none of the source's versions go with it."""
    version_id = None if body is None else body.version_id
    if version_id is None:
        # The draft as editors see it: an open guide room saves first.
        await content_rooms.flush(project_id)
    try:
        new_project = db.projects.clone_project(
            project_id, owner_id=jwt.user_id, version_id=version_id
        )
    except NoResultFound as exc:
        raise HTTPException(status_code=404, detail="No such version") from exc

    # The email and consent attachments live on disk, outside the cloned
    # rows. A clone whose templates reference files that were never copied is
    # worse than a failed clone, so undo the database side if the copy fails.
    try:
        project_storage.copy_project_attachments(project_id, new_project.id)
    except OSError as exc:
        db.projects.delete_project(new_project.id)
        project_storage.delete_project_storage([new_project.id])
        raise HTTPException(
            status_code=500,
            detail="Failed to copy the project's attachments.",
        ) from exc

    return new_project


@router.get(
    "/projects/{project_id}/available_languages",
    description="List the languages the project offers",
    response_model=list[ProjectLanguage],
)
async def get_project_languages(
    project_id: UUID4,
    db: DBSession,
    source: RespondentContent,
):
    """The languages respondents can pick: the published version's. A 409
    until the project is published."""
    return db.projects.get_available_languages_optimized(project_id, source)


@router.post(
    "/projects/{project_id}/available_languages",
    description="Offer another language, optionally machine-translating the content into it",
    response_model=list[ProjectLanguage],
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

    await content_rooms.flush(project_id)
    available_languages = await db.projects.add_project_language(
        project_id, language, translate
    )

    return available_languages


@router.delete(
    "/projects/{project_id}/available_languages",
    description="Remove project language",
    response_model=list[ProjectLanguage],
)
async def remove_project_language(
    project_id: UUID4,
    language: Annotated[LanguageCode, Body()],
    db: DBSession,
    jwt: DemoToken,
    _: ProjectEditor,
):
    try:
        available_languages = db.projects.remove_project_language(project_id, language)
    except NoResultFound as exc:
        raise HTTPException(
            status_code=404, detail="The project has no localization for that language."
        ) from exc
    # Its consent PDFs go once the published version does not offer it
    # either: until the next publish, its respondents still get them.
    prune_consent_attachments(db, project_id)

    return available_languages


@router.put(
    "/projects/{project_id}/available_languages/{language}/in_progress",
    description="Mark a language as still being translated, or as ready",
    response_model=list[ProjectLanguage],
)
async def set_language_in_progress(
    project_id: UUID4,
    language: LanguageCode,
    in_progress: Annotated[bool, Body(embed=True)],
    db: DBSession,
    jwt: DemoToken,
    _: ProjectEditor,
):
    """A language in progress stays in the draft, for editing and test
    interviews, but publishing leaves it out: respondents are not offered it,
    and its missing text does not block publishing. Not the default (409)."""
    try:
        return db.projects.set_language_in_progress(project_id, language, in_progress)
    except NoResultFound as exc:
        raise HTTPException(
            status_code=404, detail="The project has no localization for that language."
        ) from exc


@router.put(
    "/projects/{project_id}/default_language",
    description="Set the project's default language",
    response_model=list[ProjectLanguage],
)
async def set_default_language(
    project_id: UUID4,
    language: Annotated[LanguageCode, Body()],
    db: DBSession,
    jwt: DemoToken,
    _: ProjectEditor,
):
    return db.projects.set_default_language(project_id, language)


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
    "/projects/{project_id}/archive",
    description="Archive or unarchive a project",
)
async def set_project_archived(
    project_id: UUID4,
    archive_request: ArchiveRequest,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectAdmin,
):
    db.projects.set_project_archived(project_id, archive_request.archived)


@router.patch(
    "/projects/{project_id}/experiment-only",
    description="Take respondents only through an experiment, or from any link",
)
async def set_project_experiment_only(
    project_id: UUID4,
    request: ExperimentOnlyRequest,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectEditor,
):
    """Editor, not admin: putting a project in an experiment takes the editor
    role, and the experiment dialog sets this alongside it."""
    db.projects.set_experiment_only(project_id, request.experiment_only)


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


@router.patch(
    "/projects/{project_id}/folder",
    description="Move a project to another folder",
)
async def move_project(
    project_id: UUID4,
    move_request: MoveProjectRequest,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectEditor,
):
    # ProjectEditor only guards the folder the project currently lives in. The
    # destination folder is not in the path, so it needs its own role check.
    if Scope.ADMIN not in {Scope(s) for s in jwt.scope.split()}:
        target_role = db.projects.get_user_role_on_folder(
            jwt.user_id, move_request.folder_id
        )
        if target_role is None:
            raise HTTPException(404, "Resource not found")
        if not target_role.includes(CollaboratorRole.EDITOR):
            raise HTTPException(403, "Requires editor role")

    try:
        db.projects.move_project(project_id, move_request.folder_id)
    except IntegrityError:
        raise HTTPException(
            409, "A project with this title already exists in the target folder"
        )


@router.get("/projects/{project_id}", description="Load projects")
async def get_project(
    project_id: UUID4,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectViewer,
) -> ProjectPublic:
    return db.projects.get_project(project_id)


@router.get("/projects/{project_id}/permissions")
async def get_project_permissions(
    project_id: UUID4,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectViewer,
) -> ProjectPermissionsPublic:
    """What the caller may do here, for a UI that would rather not offer an
    action the API is going to refuse. Never a substitute for the checks the
    other endpoints make."""
    return ProjectPermissionsPublic(
        role=db.projects.get_user_role_on_project(jwt.user_id, project_id),
        is_owner=db.projects.is_project_owner(jwt.user_id, project_id),
        can_moderate=db.projects.can_moderate_project(
            jwt.user_id, project_id, jwt.scope
        ),
    )


def _localization_or_404(
    db: DBSession,
    project_id: UUID4,
    lang: LanguageCode,
    source: ContentSource = "draft",
):
    try:
        return db.projects.get_project_localization(project_id, lang, source=source)
    except NoResultFound as exc:
        raise HTTPException(
            status_code=404,
            detail="The project has no localization for that language.",
        ) from exc


@router.get("/projects/{project_id}/guide")
async def get_localized_guide(
    project_id: UUID4,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectViewer,
    response: Response,
) -> LocalizedInterviewGuide:
    """The guide with its text in every language: what the editor edits.
    `ETag` is the guide's revision, to send back as `If-Match`."""
    set_etag(response, db.projects.get_revision(project_id, ContentPart.GUIDE))
    return db.projects.get_guide(project_id)


@router.get("/projects/{project_id}/content/{part}/doc")
async def get_content_doc(
    project_id: UUID4,
    part: LivePart,
    jwt: DemoToken,
    _: ProjectViewer,
) -> ContentDocPublic:
    """What editors see of a co-edited part, valid or not: for its editor to
    open on. `GET /guide` and the like serve the last valid one."""
    data, errors = await content_rooms.editing(project_id, part)
    return ContentDocPublic(data=data, errors=errors or [])


@router.put("/projects/{project_id}/guide", responses=STALE_REVISION)
async def update_guide(
    project_id: UUID4,
    guide: LocalizedInterviewGuide,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectEditor,
    revision: IfMatch,
    response: Response,
) -> LocalizedInterviewGuide:
    """Save the guide. A 412 if the guide changed since `If-Match`'s
    revision; the new revision comes back as `ETag`."""
    db.versions.check_revision(project_id, revision, ContentPart.GUIDE)
    # Not validators on the guide: guides and interview copies saved before
    # these checks existed must still load. The condition order is what keeps
    # `shuffle`'s fallback to the authored order valid. The structure is the
    # same in every language, so one rendering answers for all of them.
    rendered = guide.render(db.projects.get_default_language(project_id))
    if save_errors := [
        *guide.empty_question_errors(),
        *image_name_errors(guide),
        *rendered.condition_order_errors(),
    ]:
        raise HTTPException(
            status_code=400,
            detail="\n".join(f"{e.message} {e.fix}" for e in save_errors),
        )

    db.projects.update_guide(project_id, guide, revision)
    set_etag(response, db.projects.get_revision(project_id, ContentPart.GUIDE))
    return guide


@router.get("/projects/{project_id}/{lang}/guide")
async def get_guide(
    project_id: UUID4,
    lang: LanguageCode,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectViewer,
) -> InterviewGuide:
    """The guide as an interview in `lang` gets it, untranslated text in the
    default language's."""
    return _localization_or_404(db, project_id, lang).interview_guide


@router.post("/projects/{project_id}/{lang}/translate")
async def translate_missing(
    project_id: UUID4,
    lang: LanguageCode,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectEditor,
    response: Response,
) -> ProjectContentPublic:
    """Machine-translate the text the guide, consent and welcome lack in
    `lang` from the default language. Text `lang` already has is kept.
    Each part's new revision is in `revisions`: they are three editors'."""
    # Translating what editors see, not what was last saved: before anything
    # here reads the draft, which would keep this session on the older one.
    await content_rooms.flush(project_id)
    _localization_or_404(db, project_id, lang)
    content = await db.projects.translate_missing(project_id, lang)
    return ProjectContentPublic(
        guide=content.guide,
        consent=content.consent,
        welcome=content.welcome,
        revisions={
            part: content.revision_of(part)
            for part in (ContentPart.GUIDE, ContentPart.CONSENT, ContentPart.WELCOME)
        },
    )


@router.post(
    "/projects/{project_id}/{lang}/guide/generate",
    description="Generate a new guide in `lang`. It replaces the guide in every "
    "language: the others keep no text and show as untranslated.",
)
async def generate_guide(
    project_id: UUID4,
    lang: LanguageCode,
    data: InterviewGuideGenerationRequest,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectEditor,
    response: Response,
) -> LocalizedInterviewGuide:
    generated = await generate_interview_guide(
        data.prompt, model=lib_settings.llm.default_model, language=lang
    )
    guide = LocalizedInterviewGuide.from_rendered({lang: generated}, structure=lang)

    # No If-Match: replacing the whole guide is what was asked for.
    db.projects.update_guide(project_id, guide)
    set_etag(response, db.projects.get_revision(project_id, ContentPart.GUIDE))

    return guide


@router.post("/projects/{project_id}/{lang}/guide/section/generate")
async def generate_guide_section(
    project_id: UUID4,
    lang: LanguageCode,
    data: QuestionSectionGenerationRequest,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectEditor,
) -> LocalizedQuestionSection:
    """A new section in `lang`, for the editor to insert. Not saved."""
    project = _localization_or_404(db, project_id, lang)

    section = await generate_section(
        data.prompt,
        model=lib_settings.llm.default_model,
        guide=project.interview_guide,
        language=lang,
    )

    return LocalizedQuestionSection.from_rendered({lang: section}, structure=lang)


@router.post("/projects/{project_id}/{lang}/guide/section/question/generate")
async def generate_section_question(
    project_id: UUID4,
    lang: LanguageCode,
    data: QuestionGenerationRequest,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectEditor,
) -> LocalizedQuestion:
    """A new question in `lang` for the section `data.section_id`, for the
    editor to insert. Not saved."""
    project = _localization_or_404(db, project_id, lang)
    guide = project.interview_guide

    section = next(
        (s for s in guide.question_sections if s.id == data.section_id), None
    )
    if section is None:
        raise AppError(
            "No such section in the guide",
            status=404,
            code=ErrorCode.SECTION_NOT_FOUND,
        )

    question = await generate_question(
        data.prompt,
        model=lib_settings.llm.default_model,
        guide=guide,
        section=section,
        max_probes_n=data.max_probes_n,
        max_probes_time=data.max_probes_time,
        language=lang,
    )

    # Built through a section: that is the level the lib merges renderings at.
    (localized,) = LocalizedQuestionSection.from_rendered(
        {lang: QuestionSection[Question](description="", questions=[question])},
        structure=lang,
    ).questions
    return localized


@router.post("/projects/{project_id}/image")
async def upload_image(
    project_id: UUID4,
    file: UploadFile,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectEditor,
    # NOTE:
    # Unused: the image's texts live in the guide. Kept so older clients that
    # send them are not refused.
    primer: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    alt: Annotated[str, Form()] = "",
) -> str:
    """Store a guide image. Returns the name to put in the question's
    `image.name`; the guide never carries the image itself."""
    filename = ensure_filename(file.filename)

    filepath = project_storage.image_path(project_id) / filename

    async with aiofiles.open(filepath, "wb") as f:
        contents = await file.read()
        await f.write(contents)

    # The name it was stored under, for the guide to refer to.
    return filename


@router.get("/projects/{project_id}/agents")
async def get_interview_agents(
    project_id: UUID4,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectViewer,
    response: Response,
) -> AgentConfigs:
    set_etag(response, db.projects.get_revision(project_id, ContentPart.AGENTS))
    return db.projects.get_agent_configs(project_id)


@router.post("/projects/{project_id}/agents", responses=STALE_REVISION)
async def create_interview_agents(
    project_id: UUID4,
    config: AgentConfigs,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectEditor,
    revision: IfMatch,
    response: Response,
):
    db.projects.update_agent_configs(
        project_id=project_id, agent_configs=config, revision=revision
    )
    set_etag(response, db.projects.get_revision(project_id, ContentPart.AGENTS))


# Labelled stand-ins for the interview-time context the instruction prompt
# expects. They let us render the template at config time so the user can see
# how their edited slots land, without needing a live interview.
# Typed loosely: spread into `generate_probing_prompt`, whose other
# parameters (the probe budget, the earlier probes) are not strings, a
# `dict[str, str]` reads as passing a string to each of them.
_PREVIEW_PLACEHOLDERS: dict[str, Any] = {
    "interview_framing": "«interview framing — filled in during the interview»",
    "section_description": "«section description — filled in during the interview»",
    "question_description": "«question description — filled in during the interview»",
    "main_question": "«main question — filled in during the interview»",
    "interview_transcript": "«transcript so far — filled in during the interview»",
    "suggested_probes": "«suggested probes for this question — included when configured»",
}

# Not passed with the rest: ProbingAgentPrompts derives `translation` from the
# language itself. Only stood in for where it derives none -- an English
# interview -- so the preview still shows where that line goes.
_PREVIEW_TRANSLATION = (
    "«interview language — included when the interview is not in English»"
)


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
    prompts.translation = prompts.translation or _PREVIEW_TRANSLATION

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


#: `InterviewConfig` fields only a caller with their preview feature may change.
_PREVIEW_CONFIG_FIELDS: dict[str, PreviewFeature] = {
    "with_audio": PreviewFeature.AUDIO,
    "probing_strategy": PreviewFeature.PROBING,
}


@router.get("/projects/{project_id}/config")
async def get_interview_config(
    project_id: UUID4,
    db: DBSession,
    source: RespondentContent,
    response: Response,
) -> InterviewConfig:
    """Which screens and features the interview has: the published version's,
    or the draft's with `?draft=true` for a viewer (the setup page, and manual
    tests through the respondent page). The draft's comes with its `ETag`."""
    if source == "draft":
        set_etag(response, db.projects.get_revision(project_id, ContentPart.SETTINGS))
    return db.projects.get_interview_config(project_id, source)


@router.get("/projects/{project_id}/models")
async def get_interview_models(
    project_id: UUID4,
    db: DBSession,
    source: RespondentContent,
) -> set[str]:
    """The models respondents' interviews run on: the published version's."""
    return {
        agent_config.model
        for agent, agent_config in db.projects.get_agent_configs(project_id, source)
        if agent in _RESPONDENT_FACING_AGENTS
    }


@router.post("/projects/{project_id}/config", responses=STALE_REVISION)
async def create_interview_config(
    project_id: UUID4,
    config: InterviewConfig,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectEditor,
    revision: IfMatch,
    response: Response,
):
    """Write the draft's interview config. Like the rest of the content it
    reaches respondents when published."""
    kept = [
        field
        for field, feature in _PREVIEW_CONFIG_FIELDS.items()
        if not jwt.has_preview(feature)
    ]
    if kept:
        # Keep what is stored rather than refusing: the setup page posts the
        # whole config, so an editor without the feature saving consent or
        # welcome on a project whose owner turned audio on must neither fail
        # nor silently switch audio back off.
        stored = db.projects.get_interview_config(project_id)
        config = config.model_copy(
            update={field: getattr(stored, field) for field in kept}
        )
    db.projects.update_interview_config(project_id, config, revision)
    set_etag(response, db.projects.get_revision(project_id, ContentPart.SETTINGS))


@router.patch("/projects/{project_id}/external_params")
async def update_external_params(
    project_id: UUID4,
    request: ExternalParamsReplace,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectEditor,
):
    db.projects.update_external_params(project_id, request.params)


@router.put("/projects/{project_id}/redirects")
async def replace_redirects(
    project_id: UUID4,
    redirects: CompletionRedirects,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectEditor,
) -> CompletionRedirects:
    """Set where respondents are sent when a distributed interview ends; an
    outcome left null redirects nowhere. Takes effect at once, interviews
    already running included: it is not part of the published content."""
    db.projects.update_redirects(project_id, redirects)
    return redirects


@router.get("/prompt-defaults")
async def get_prompt_defaults(jwt: DemoToken) -> ProbingPromptSlots:
    """Default values for the editable probing-agent prompt slots.

    Project-specific overrides live on the probing agent config
    (``AgentConfigs.probing.prompt_slots``) and are read/written through the
    ``/agents`` endpoints. The frontend uses these defaults to show the effective
    text for any slot a project has not overridden.
    """
    return DEFAULT_PROBING_SLOTS


@router.get("/security-policy-defaults")
async def get_security_policy_defaults(jwt: DemoToken) -> SecurityPolicy:
    """The security policy a project gets unless it configures its own.

    A project's policy lives on ``AgentConfigs.security.policy`` and is read and
    written through the ``/agents`` endpoints. The frontend uses this to reset
    an edited policy.
    """
    return default_security_policy()


# NOTE: Doesn't require auth since it's not sensitive and has to be read
# from the interview page by visitors
@router.get("/projects/{project_id}/guide/consent/{language}")
async def get_consent(
    project_id: UUID4,
    db: DBSession,
    language: LanguageCode,
    source: RespondentContent,
) -> Consent | None:
    """The consent as respondents in `language` see it: the published
    version's, a 409 until there is one."""
    # A language the project does not offer is a 404, distinct from a 200 `null` meaning the
    # project has no consent configured. The interview page relies on that
    # distinction: it may only start an interview without a consent screen
    # for the second case.
    return _localization_or_404(db, project_id, language, source).consent


@router.get("/projects/{project_id}/consent")
async def get_localized_consent(
    project_id: UUID4,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectViewer,
    response: Response,
) -> LocalizedConsent | None:
    """The consent with its text in every language: what the editor edits."""
    set_etag(response, db.projects.get_revision(project_id, ContentPart.CONSENT))
    return db.projects.get_consent(project_id)


@router.put("/projects/{project_id}/consent", responses=STALE_REVISION)
async def update_consent(
    project_id: UUID4,
    consent: LocalizedConsent,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectEditor,
    revision: IfMatch,
    response: Response,
) -> LocalizedConsent:
    db.projects.update_consent(project_id, consent, revision)
    set_etag(response, db.projects.get_revision(project_id, ContentPart.CONSENT))
    return consent


# NOTE: Doesn't require auth since it's not sensitive and has to be read
# from the interview page by visitors
@router.get("/projects/{project_id}/guide/welcome/{language}")
async def get_welcome(
    project_id: UUID4,
    language: LanguageCode,
    db: DBSession,
    source: RespondentContent,
) -> Welcome | None:
    """The welcome screen as respondents in `language` see it: the published
    version's, a 409 until there is one."""
    # See `get_consent`: a language not offered is a 404, not a 500.
    return _localization_or_404(db, project_id, language, source).welcome


@router.get("/projects/{project_id}/welcome")
async def get_localized_welcome(
    project_id: UUID4,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectViewer,
    response: Response,
) -> LocalizedWelcome | None:
    """The welcome screen with its text in every language: what the editor
    edits."""
    set_etag(response, db.projects.get_revision(project_id, ContentPart.WELCOME))
    return db.projects.get_welcome(project_id)


@router.put("/projects/{project_id}/welcome", responses=STALE_REVISION)
async def update_welcome(
    project_id: UUID4,
    welcome: LocalizedWelcome,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectEditor,
    revision: IfMatch,
    response: Response,
) -> LocalizedWelcome:
    """Save the welcome screen. Upload its video first
    (`POST .../welcome/video`) and pass the returned name as
    `video_file_name`."""
    db.projects.update_welcome(project_id, welcome, revision)
    set_etag(response, db.projects.get_revision(project_id, ContentPart.WELCOME))
    return welcome


#: What browsers play in the welcome screen's video element.
WELCOME_VIDEO_EXTENSIONS = frozenset({".mp4", ".webm"})


@router.post("/projects/{project_id}/welcome/video")
async def upload_welcome_video(
    project_id: UUID4,
    video: UploadFile,
    jwt: DemoToken,
    _: ProjectEditor,
) -> str:
    """Store a welcome video, shared by every language. Returns the file name
    to save as the welcome's `video_file_name`."""
    # The client's name becomes the stored file's, so it must be a bare one:
    # "../" in it would write outside the project's videos.
    name = safe_filename(video.filename)
    if Path(name).suffix.lower() not in WELCOME_VIDEO_EXTENSIONS:
        allowed = ", ".join(sorted(WELCOME_VIDEO_EXTENSIONS))
        raise HTTPException(
            status_code=400, detail=f"'{name}' is not a video. Allowed: {allowed}."
        )

    async with aiofiles.open(project_storage.video_path(project_id) / name, "wb") as f:
        while chunk := await video.read(1024 * 1024):
            await f.write(chunk)

    return name


def _filter_kwargs(filters: InterviewFilterParams) -> dict:
    """The list's filters as the interview repository takes them.

    Shared by the list and the bulk actions, so "all matching" in a bulk
    request cannot drift from what the list showed -- the scope default in
    particular.
    """
    return {
        "interview_types": filters.interview_types
        if filters.interview_types is not None
        else [InterviewType.DISTRIBUTED],
        "selected_types": filters.types,
        "statuses": filters.statuses,
        "languages": filters.languages,
        "created_from": filters.created_from,
        "created_to": filters.created_to,
        "completed": filters.completed,
        "search": filters.search,
        "pid": filters.pid,
        "reported": filters.reported,
        "intervened": filters.intervened,
        "versions": filters.versions,
    }


def _refuse_distributed_to_demo(
    db: DBSession, jwt: AuthToken, project_id: UUID4, interview_ids: list[UUID4]
) -> None:
    """Demo accounts see test interviews only: the interview data of a
    project they collaborate on is not theirs to read with demo access. The
    test results page reads tests through the same routes."""
    if is_demo(jwt) and db.interviews.any_of_type(
        project_id, interview_ids, InterviewType.DISTRIBUTED
    ):
        raise demo_restricted()


def _selected_interview_ids(
    db: DBSession, project_id: UUID4, selection: InterviewSelection
) -> list[UUID4]:
    if selection.filters is None:
        return selection.interview_ids or []
    return db.interviews.get_interview_ids(
        project_id, **_filter_kwargs(selection.filters)
    )


@router.get("/projects/{project_id}/interviews")
async def get_interviews(
    project_id: UUID4,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectViewer,
    paginated_query: Annotated[PageParams, Query()],
    interview_filters: Annotated[InterviewFilterParams, Query()],
) -> InterviewListResponse:
    """One page of interviews, with the filter options that fit the query.

    `interview_types` is the scope of the list: the interviews page passes
    nothing and gets distributed interviews, the test results page passes the
    two test types. `types` is the user narrowing that scope from the Type
    filter, and unlike the scope it is dropped when counting the type facet.

    `pid` narrows the list to a single participant and is matched exactly --
    it backs the "view interviews" link from the participants table, where the
    pid is already known.

    `reported` narrows to interviews with at least one message flagged for
    review -- reported by the respondent or by the safety check -- or to those
    with none. `intervened`
    does the same for the safety check, counted off the transcript, so test
    interviews -- which are never flagged for review -- can be filtered too.
    """
    if paginated_query.column not in SORTABLE_INTERVIEW_COLUMNS:
        # The repository raises ValueError, which would surface as a 500. A
        # column the client should never have offered is a bad request.
        raise HTTPException(
            status_code=400,
            detail=f"Cannot sort by {paginated_query.column!r}. Sortable columns: "
            f"{', '.join(sorted(SORTABLE_INTERVIEW_COLUMNS))}.",
        )

    # The page and the facet counts must be built from exactly the same
    # filters, or the dropdown would offer options the list contradicts.
    filters = _filter_kwargs(interview_filters)
    if is_demo(jwt) and InterviewType.DISTRIBUTED in filters["interview_types"]:
        # See `_refuse_distributed_to_demo`.
        raise demo_restricted()

    interviews, total = db.interviews.get_interviews(
        project_id,
        offset=paginated_query.offset,
        limit=paginated_query.limit,
        sorting_column=paginated_query.column,
        sorting_order=paginated_query.order,
        **filters,
    )
    facets = db.interviews.get_interview_facets(project_id, **filters)
    versions = facets.pop("version", {})
    version_names = db.interviews.get_version_names(
        project_id, [int(no) for no in versions]
    )

    return InterviewListResponse(
        total=total,
        items=list(interviews),
        facets=InterviewFacets(
            **{
                name: [
                    FacetCount(value=value, count=count)
                    for value, count in sorted(counts.items())
                ]
                for name, counts in facets.items()
            },
            # Newest first, by number rather than as strings ("10" < "9").
            version=[
                FacetCount(value=no, count=count, label=version_names.get(int(no)))
                for no, count in sorted(
                    versions.items(), key=lambda item: int(item[0]), reverse=True
                )
            ],
        ),
    )


@router.delete("/projects/{project_id}/interviews")
async def delete_interviews(
    delete_request: InterviewSelection,
    project_id: UUID4,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectAdmin,
):
    interview_ids = _selected_interview_ids(db, project_id, delete_request)
    _refuse_distributed_to_demo(db, jwt, project_id, interview_ids)
    await session_manager.end([project_id], interview_ids)
    # The ids actually deleted: those not of this project were ignored, and
    # their files are not this request's to remove.
    deleted = db.interviews.delete_interviews(project_id, interview_ids)
    interview_storage.delete_interview_storage(deleted)


# ============ Interview resume links ============
#
# A resume link is the only way to continue a specific interview in a browser
# that has neither the `interview_token` cookie nor the frontend's localStorage
# entry. It is therefore a bearer credential for one transcript, and is issued
# one interview at a time, by hand, rather than mailed out with every
# distribution link -- see InterviewResumeTokenTable.


def _resume_link_public(
    token: InterviewResumeTokenTable | None,
) -> InterviewResumeLinkPublic | None:
    if token is None:
        return None
    # Every datetime is normalised on the way out: these columns are
    # timezone-less, and a naive ISO string is parsed by the browser as *local*
    # time, so a viewer in another timezone would be shown the wrong expiry.
    return InterviewResumeLinkPublic(
        created_at=as_aware(token.created_at),
        expires_at=as_aware(token.expires_at),
        redeemed_at=as_aware(token.redeemed_at) if token.redeemed_at else None,
        revoked_at=as_aware(token.revoked_at) if token.revoked_at else None,
        redeemable=(
            token.redeemed_at is None
            and token.revoked_at is None
            and as_aware(token.expires_at) > now()
        ),
    )


def _resume_target_or_404(db: DBSession, project_id: UUID4, interview_id: UUID4):
    try:
        return db.interviews.get_resume_target(project_id, interview_id)
    except NoResultFound:
        raise HTTPException(status_code=404, detail="Interview not found.")


@router.get("/projects/{project_id}/interviews/{interview_id}/resume-link")
async def get_interview_resume_link(
    project_id: UUID4,
    interview_id: UUID4,
    db: DBSession,
    jwt: UserToken,
    _: ProjectEditor,
) -> InterviewResumeLinkPublic | None:
    """Whether a resume link is outstanding for this interview, and its state.

    Never the URL itself -- only a hash is stored, so there is nothing to give
    back. Editor rather than viewer for symmetry with issuing one: knowing a
    live credential exists is part of managing it.
    """
    _resume_target_or_404(db, project_id, interview_id)
    return _resume_link_public(db.interviews.get_resume_token(interview_id))


@router.post("/projects/{project_id}/interviews/{interview_id}/resume-link")
async def create_interview_resume_link(
    project_id: UUID4,
    interview_id: UUID4,
    db: DBSession,
    jwt: UserToken,
    _: ProjectEditor,
) -> InterviewResumeLinkResponse:
    """Mint a one-time resume link, replacing any outstanding one.

    Editor, not viewer: every other route on this page is viewer-readable, but
    a member who can mint resume links can walk into any transcript in the
    project, so this is a write.

    The URL is returned exactly once. Only its hash is stored, so a member who
    loses it has to issue a new one -- which revokes this one.
    """
    interview = _resume_target_or_404(db, project_id, interview_id)

    if interview.status == InterviewStatus.COMPLETED:
        raise HTTPException(
            status_code=409,
            detail="This interview is already complete, so there is nothing to resume.",
        )

    project = db.projects.get_project(project_id)
    if project.owner.scope == Scope.DEMO:
        raise HTTPException(
            status_code=403,
            detail="Demo projects cannot issue resume links.",
        )

    # Opting out means "stop contacting me", and a resume link exists to be
    # sent. Redemption is left alone deliberately: a link already in someone's
    # hands should still work if they choose to finish.
    link = interview.project_participant
    if link is not None and not link.participant.participating:
        raise HTTPException(
            status_code=409,
            detail="This participant has opted out, so no resume link can be issued.",
        )

    raw_token = generate_resume_token()
    expires_at = (
        now() + app_settings.app.interview_resume_link_expiration.to_timedelta()
    )
    token = db.interviews.create_resume_token(
        interview_id=interview_id,
        token_hash=hash_token(raw_token),
        expires_at=expires_at,
        created_by=jwt.user_id,
    )

    # A live credential for a transcript: record who minted it, as the email
    # bundle export does for participant data.
    logger.info(
        "interview resume link issued: interview=%s project=%s user=%s token=%s expires=%s",
        interview_id,
        project_id,
        jwt.user_id,
        token.id,
        expires_at,
    )

    base_url = app_settings.app.public_url
    return InterviewResumeLinkResponse(
        url=f"{base_url}/interview/resume/{raw_token}",
        expires_at=expires_at,
    )


@router.delete("/projects/{project_id}/interviews/{interview_id}/resume-link")
async def revoke_interview_resume_link(
    project_id: UUID4,
    interview_id: UUID4,
    db: DBSession,
    jwt: UserToken,
    _: ProjectEditor,
) -> InterviewResumeLinkPublic | None:
    """Kill any still-redeemable link for this interview.

    Already-redeemed rows are left as they are; revoking them would erase when
    they were used.
    """
    _resume_target_or_404(db, project_id, interview_id)
    revoked = db.interviews.revoke_resume_tokens(interview_id)
    if revoked:
        logger.info(
            "interview resume link revoked: interview=%s project=%s user=%s count=%d",
            interview_id,
            project_id,
            jwt.user_id,
            revoked,
        )
    return _resume_link_public(db.interviews.get_resume_token(interview_id))


@router.post("/projects/{project_id}/test_share")
async def create_test_share_link(
    project_id: UUID4,
    db: DBSession,
    jwt: UserToken,
    _: ProjectEditor,
    body: TestShareRequest | None = None,
) -> TestShareLinkResponse:
    """A link that lets anyone try the project's draft as a manual test,
    without an account: for piloting the guide before publishing it.

    With `redirect`, its interviews end on the project's completion
    redirects, as distributed ones do, so a panel's return links can be
    piloted too.

    Not for demo accounts, nor on a project a demo account owns: those may
    not distribute, and a link anyone can open would be distributing.
    """
    if db.projects.is_project_owner_demo_user(project_id):
        raise demo_restricted()
    nonce = db.projects.ensure_test_share_nonce(project_id)
    redirect = body is not None and body.redirect
    token, expires_at = create_test_share_token(project_id, nonce, redirect)

    # A credential to unreleased work: record who minted it.
    logger.info(
        "test share link issued: project=%s user=%s expires=%s redirect=%s",
        project_id,
        jwt.user_id,
        expires_at,
        redirect,
    )

    query = urlencode(
        {"id": str(project_id), "interview_type": "manual_test", "share": token}
    )
    return TestShareLinkResponse(
        url=f"{app_settings.app.public_url}/interview?{query}",
        expires_at=expires_at,
    )


@router.delete("/projects/{project_id}/test_share", status_code=204)
async def revoke_test_share_links(
    project_id: UUID4,
    db: DBSession,
    jwt: UserToken,
    _: ProjectEditor,
) -> None:
    """End every shared test link of the project handed out so far.
    Interviews already started from one carry on."""
    db.projects.revoke_test_shares(project_id)
    logger.info("test share links revoked: project=%s user=%s", project_id, jwt.user_id)


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
    _refuse_distributed_to_demo(db, jwt, project_id, [interview_id])
    return db.interviews.get_messages(interview_id, project_id)


@router.get(
    "/projects/{project_id}/interviews/{interview_id}/audio/{filename}",
    response_class=Response,
    responses=file_response("audio/wav"),
)
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
    _refuse_distributed_to_demo(db, jwt, project_id, [interview_id])

    audio_path = (
        interview_storage.audio_path(interview_id)
        / Path(filename).name  # strips any path components
    )
    if not audio_path.is_file():
        raise HTTPException(status_code=404, detail="Recording not found")

    return FileResponse(audio_path, media_type="audio/wav")


@router.post(
    "/projects/{project_id}/interviews/messages/export",
    response_class=Response,
    responses=file_response(*MEDIA_TYPES.values()),
)
async def export_messages(
    export_request: ExportMessagesRequest,
    project_id: UUID4,
    db: DBSession,
    jwt: DemoToken,
    _: ProjectViewer,
):
    interview_ids = _selected_interview_ids(db, project_id, export_request)
    _refuse_distributed_to_demo(db, jwt, project_id, interview_ids)
    summaries = db.interviews.get_interview_summaries(project_id, interview_ids)
    param_values = db.projects.get_external_param_values_for_interviews(
        project_id, interview_ids
    )
    interviews = [
        InterviewExport(
            summary=summaries[interview_id],
            messages=db.interviews.get_messages(interview_id, project_id),
            external_params=param_values.get(interview_id) or {},
        )
        for interview_id in interview_ids
        # Gone since the selection was made, or never in this project.
        if interview_id in summaries
    ]
    tz = (
        ZoneInfo(export_request.timezone)
        if export_request.timezone
        else lib_settings.tzinfo
    )

    # Laying out a PDF of a hundred transcripts takes seconds of CPU; off the
    # event loop, so it does not stall every other request meanwhile.
    export = await run_in_threadpool(
        export_interviews,
        interviews,
        project_id=project_id,
        format=export_request.format,
        archive=export_request.archive,
        external_params=db.projects.get_external_params(project_id),
        tz=tz,
    )
    return Response(
        content=export.content,
        media_type=export.media_type,
        headers={"Content-Disposition": f'attachment; filename="{export.filename}"'},
    )


@router.get(
    "/projects/{project_id}/qr.png",
    response_class=Response,
    responses=file_response("image/png"),
)
async def generate_project_qr(
    request: Request,
    project_id: UUID4,
    jwt: DemoToken,
    _: ProjectViewer,
):
    # The page's own query string is the link's: a language, or values for the
    # project's external params. Such a QR is generated on demand rather than
    # cached, since every combination would otherwise leave a file behind.
    extra = [(k, v) for k, v in request.query_params.multi_items() if k != "id"]
    if extra:
        query = urlencode([("id", str(project_id)), *extra])
        img_data = generate_qr_img(f"{request.base_url}interview?{query}")
        return StreamingResponse(io.BytesIO(img_data), media_type="image/png")

    file_path = project_storage.qr_code_path(project_id) / "interview.png"

    if not file_path.exists():
        interview_url = str(request.base_url) + f"interview?id={project_id}"
        img_data = generate_qr_img(str(interview_url), file_path=file_path)
        # TODO: Why not just return FileResponse after file has been created?
        return StreamingResponse(io.BytesIO(img_data), media_type="image/png")

    return FileResponse(file_path)


@router.post("/projects/{project_id}/owner")
async def check_project_owner(
    project_id: UUID4,
    db: DBSession,
) -> bool:
    return db.projects.is_project_owner_demo_user(project_id)
