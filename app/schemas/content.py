"""A project's content: its versions, rendering and generation."""

from __future__ import annotations

from datetime import datetime

from pydantic import (
    UUID4,
    Field,
)

from ainterviewer.agents.config import AgentConfigs
from ainterviewer.interview_guides import (
    InterviewGuide,
    LocalizedConsent,
    LocalizedInterviewGuide,
    LocalizedWelcome,
)
from ainterviewer.interview_guides.extra import Consent, Welcome
from ainterviewer.types import (
    LanguageCode,
)

from ..db.types import (
    ContentPart,
    ContentVersionKind,
)
from ..db.versioning import BumpName
from ..types import (
    ParticipantEmails,
)
from .common import PromptRequest, Schema


class ProjectLocalizationPublic(Schema):
    """A project as respondents in one language get it: its content rendered
    in `language`, untranslated text in the default language's. Built by
    `ProjectRepository.get_project_localization`; nothing stores it."""

    project_id: UUID4
    language: LanguageCode
    consent: Consent | None
    welcome: Welcome | None
    interview_guide: InterviewGuide
    prompt_overrides: dict[str, str]
    agent_configs: AgentConfigs
    #: The version rendered, None when it was the draft.
    content_version_id: UUID4 | None = None


class ContentVersionSummaryPublic(Schema):
    """One version of a project's content, as the history lists it."""

    id: UUID4
    version_no: int
    kind: ContentVersionKind
    #: "2.1.0" on published versions, None on every other kind.
    semver: str | None
    label: str | None
    created_at: datetime
    created_by_id: UUID4 | None
    #: The author's name, None once the user is deleted.
    created_by_name: str | None
    is_published: bool
    languages: list[LanguageCode]
    default_language: LanguageCode
    #: Of `languages`, those still being translated: not offered to
    #: respondents when this version is the published one.
    languages_in_progress: list[LanguageCode]


class ContentVersionPublic(ContentVersionSummaryPublic):
    """A version with its content, for previewing it read-only."""

    guide: LocalizedInterviewGuide
    consent: LocalizedConsent | None
    welcome: LocalizedWelcome | None
    emails: ParticipantEmails
    agent_configs: AgentConfigs
    prompt_overrides: dict[str, str]


class ContentStatus(Schema):
    """Where the draft stands against what respondents get."""

    #: The draft's revision, what `If-Match` must carry.
    revision: int
    published: ContentVersionSummaryPublic | None
    #: The draft differs from the published version (always true while the
    #: project has never been published).
    has_unpublished_changes: bool
    #: How far the draft is from the published version, so what publishing
    #: it now would bump ("none" when it is the same). See app/db/versioning.py.
    pending_bump: BumpName
    #: The version publishing it now would get, with `pending_bump`.
    next_semver: str
    #: Languages in the draft that publishing leaves out: still in progress.
    languages_in_progress: list[LanguageCode]
    #: Per offered language, the paths of text it lacks. Publishing is
    #: refused while this is not empty.
    missing: dict[LanguageCode, list[str]]
    #: Per co-edited part (guide, consent, welcome), why what editors have
    #: written is not valid, while it is not (`contentdoc.errors`). The draft
    #: keeps the last valid one, and publishing is refused meanwhile.
    doc_errors: dict[ContentPart, list[str]] = {}


class InterviewGuideGenerationRequest(PromptRequest): ...


class QuestionSectionGenerationRequest(PromptRequest): ...


class QuestionGenerationRequest(PromptRequest):
    section_id: UUID4
    max_probes_n: int | None = None
    max_probes_time: int | None = None


class AssistanceChatRequest(Schema):
    prompt: str
    guide: InterviewGuide


class ProbingPromptPreview(Schema):
    """Rendered probing-agent prompts with the project's editable slots injected.

    Interview-time context (transcript, framing, etc.) is shown as labelled
    placeholders since those values only exist while an interview is running.
    """

    system: str
    instruction: str


class ProjectContentPublic(Schema):
    """A project's guide, consent and welcome with their text per language."""

    guide: LocalizedInterviewGuide
    consent: LocalizedConsent | None
    welcome: LocalizedWelcome | None
    #: Each part's revision, for its editor's next `If-Match`.
    revisions: dict[ContentPart, int]


class ContentVersionCreate(Schema):
    label: str | None = Field(default=None, max_length=200)
    #: Publish it to respondents, or only keep it in the history.
    publish: bool = False
    #: Raise the computed bump when publishing; lowering it is refused.
    bump: BumpName | None = None
