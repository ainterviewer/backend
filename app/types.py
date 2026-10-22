"""The domain's enums and value types, shared by the tables, the schemas
and the services. SQLAlchemy column types are in `app.db.types`."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal, NotRequired, TypedDict

from fastapi import WebSocket
from pydantic import BaseModel, Field, model_validator

from ainterviewer.interfaces import SecurityOverride
from ainterviewer.interview_guides import LocalizedText
from ainterviewer.interview_guides.types import SecurityAction
from ainterviewer.types import LanguageCode


class Scope(StrEnum):
    ADMIN = "admin"
    USER = "user"
    DEMO = "demo"

    def includes(self, other: Scope) -> bool:
        """Check if this scope includes the permissions of another scope."""
        return other in _SCOPE_HIERARCHY[self]


_SCOPE_HIERARCHY: dict[Scope, set[Scope]] = {
    Scope.ADMIN: {Scope.ADMIN, Scope.USER, Scope.DEMO},
    Scope.USER: {Scope.USER, Scope.DEMO},
    Scope.DEMO: {Scope.DEMO},
}


class CollaboratorRole(StrEnum):
    VIEWER = "viewer"
    ANNOTATOR = "annotator"
    EDITOR = "editor"
    ADMIN = "admin"

    def includes(self, other: CollaboratorRole) -> bool:
        """Check if this scope includes the permissions of another scope."""
        return other in _COLLABORATOR_HIERARCHY[self]


_COLLABORATOR_HIERARCHY: dict[CollaboratorRole, set[CollaboratorRole]] = {
    CollaboratorRole.ADMIN: {
        CollaboratorRole.ADMIN,
        CollaboratorRole.EDITOR,
        CollaboratorRole.ANNOTATOR,
        CollaboratorRole.VIEWER,
    },
    CollaboratorRole.EDITOR: {
        CollaboratorRole.EDITOR,
        CollaboratorRole.ANNOTATOR,
        CollaboratorRole.VIEWER,
    },
    CollaboratorRole.ANNOTATOR: {
        CollaboratorRole.ANNOTATOR,
        CollaboratorRole.VIEWER,
    },
    CollaboratorRole.VIEWER: {
        CollaboratorRole.VIEWER,
    },
}


class WebSocketUsers(TypedDict):
    user: NotRequired[WebSocket]
    interviewer: NotRequired[WebSocket]


class WebSocketConversation(TypedDict):
    message_count: int
    users: WebSocketUsers


class TurnRole(StrEnum):
    """Who is speaking in a rendered chunk of interview text.

    Coarser than `MessageRole` on purpose: a reader of a search result cares
    whether a line was asked or answered, not whether the asking was done by
    the agent or by a scripted guide message.
    """

    INTERVIEWER = "interviewer"
    RESPONDENT = "respondent"


class GroupKind(StrEnum):
    """What a cluster map's points are grouped by.

    `CLUSTER` is what HDBSCAN found; the rest are declared rather than
    discovered, and are the baseline the clusters are worth reading against. If
    colouring by one of them reproduces the clustering, the clustering found
    that -- the interview guide, or the respondent's language -- and not a
    theme.
    """

    CLUSTER = "cluster"
    QUESTION = "question"
    SECTION = "section"
    LANGUAGE = "language"


class Projection(StrEnum):
    """How embedding vectors are reduced before clustering and plotting.

    Both run HDBSCAN in exactly the space the scatter shows, so a cluster can
    never be a shape the picture does not contain -- they differ in what that
    space is.

    `PCA` is linear and fast: 50 components, milliseconds, and the two plotted
    axes carry a reported share of the total variance. Its weakness is that
    50 dimensions of text embedding are still high enough for distances to
    concentrate, so HDBSCAN tends to find few large blobs.

    `UMAP` reduces to 2 non-linear dimensions, which separates neighbourhoods
    far more sharply and is usually the readable picture. Three costs: it is
    seconds rather than milliseconds (plus a one-off numba compile on the first
    call of a process), there is no variance ratio to report, and clustering in
    two dimensions can split one topic into several. Read `question_purity`
    and the representatives before trusting a split.
    """

    PCA = "pca"
    UMAP = "umap"


class WSChatRole(StrEnum):
    USER = "user"
    INTERVIEWER = "interviewer"


class ProjectStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class TestRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ParticipantEmail(BaseModel):
    """One email to participants, its subject and HTML body per language."""

    subject: LocalizedText = Field(default_factory=dict)
    template: LocalizedText = Field(default_factory=dict)

    def get(self, language: LanguageCode) -> tuple[str | None, str | None]:
        """(subject, template) in `language`, None where it has none. No
        fallback: sending picks one per participant (see
        `api/dashboard/participants.py`)."""
        return self.subject.get(language) or None, self.template.get(language) or None

    def set(
        self, language: LanguageCode, subject: str | None, template: str | None
    ) -> ParticipantEmail:
        """A copy with `language`'s subject and template replaced; None clears."""
        updated = self.model_copy(deep=True)
        for texts, value in ((updated.subject, subject), (updated.template, template)):
            if value:
                texts[language] = value
            else:
                texts.pop(language, None)
        return updated


class ParticipantEmails(BaseModel):
    """The emails a project sends its participants."""

    invite: ParticipantEmail = Field(default_factory=ParticipantEmail)
    reminder: ParticipantEmail = Field(default_factory=ParticipantEmail)

    def texts(self):
        for name in ("invite", "reminder"):
            email: ParticipantEmail = getattr(self, name)
            yield f"{name}.subject", email.subject
            yield f"{name}.template", email.template


class ExternalParam(BaseModel):
    """Definition of a single external URL query parameter."""

    name: str = Field(
        ...,
        pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$",
        description="Query parameter name (valid identifier)",
    )
    # TODO: Allow lists?
    type: Literal["str", "int", "float", "bool", "enum"] = Field(
        ...,
        description="Value type for the parameter",
    )
    required: bool = True
    default: str | int | float | bool | None = None
    options: list[str] | None = Field(
        None,
        description="Allowed values (required when type='enum')",
    )
    description: str | None = Field(
        None,
        description="Human-readable description of the parameter's purpose",
    )

    @model_validator(mode="after")
    def validate_enum_options(self) -> ExternalParam:
        if self.type == "enum":
            if not self.options:
                raise ValueError("options must be non-empty when type is 'enum'")
            if self.default is not None and str(self.default) not in self.options:
                raise ValueError(
                    f"default '{self.default}' must be one of {self.options}"
                )
        return self


def build_external_params_model(
    params: list[ExternalParam],
) -> type[BaseModel]:
    """Build a dynamic Pydantic model from stored ExternalParam definitions.

    Used at runtime to validate incoming interview query parameters against the
    project's configured external params schema.
    """
    from enum import Enum as PyEnum

    from pydantic import create_model

    type_map: dict[str, type] = {
        "str": str,
        "int": int,
        "float": float,
        "bool": bool,
    }

    fields: dict[str, Any] = {}
    for param in params:
        if param.type == "enum":
            enum_cls = PyEnum(param.name, {v: v for v in param.options})  # ty:ignore[mismatched-type-name, not-iterable]
            field_type = enum_cls
        else:
            field_type = type_map[param.type]

        if param.required and param.default is None:
            fields[param.name] = (field_type, ...)
        elif param.default is not None:
            fields[param.name] = (field_type, param.default)
        else:
            fields[param.name] = (field_type | None, None)

    return create_model("ExternalParams", **fields)


class CodeKind(StrEnum):
    """What a code *is*, which decides what applying it to a passage produces.

    ``TAG`` is the ordinary case: the code either applies or it does not.
    ``SCORE`` asks the coder for a number in the code's own range, for codes
    that are degrees of something rather than presences of it. ``GROUP`` is
    neither -- it organises the branch under it and is never applied, which is
    what keeps a parent's count from pretending to include its children's.
    """

    GROUP = "group"
    TAG = "tag"
    SCORE = "score"


#: The colours a codebook starts with: hues for top-level codes, checked
#: pairwise for colour-vision deficiency. A starting point rather than the set
#: -- the palette is part of the codebook document from there on, and an
#: analyst can edit it, add to it and cut it down.
DEFAULT_PALETTE: tuple[str, ...] = (
    "#0f766e",
    "#b45309",
    "#4338ca",
    "#be185d",
    "#0369a1",
    "#4d7c0f",
    "#7c3aed",
    "#a16207",
)


class AccessRequestStatus(StrEnum):
    WAITING = "waiting"
    FULFILLED = "fulfilled"
    DENIED = "denied"


class ReportReason(StrEnum):
    """Why a respondent reported an interviewer's question.

    A closed list rather than free text because the reason is what the review
    queues are filtered and counted by, and because a respondent who is being
    asked something offensive should be able to say so in one tap. ``OTHER``
    is what makes the list honest: it exists so that the four options never
    have to be stretched to cover something they do not, and a report carrying
    it is expected to carry a comment as well.
    """

    INAPPROPRIATE = "inappropriate"
    OFFENSIVE = "offensive"
    IRRELEVANT = "irrelevant"
    OTHER = "other"


class ReportSource(StrEnum):
    """Who flagged a message: the respondent, or the interview's safety check.

    Both land in the same queues because they ask a reviewer the same thing --
    look at this part of the transcript -- and need the same two review tracks.
    A respondent's report names an interviewer question and carries a
    ``ReportReason``; a safety flag names the respondent answer the check
    intervened on and carries a ``SafetyFlag`` instead.
    """

    RESPONDENT = "respondent"
    SAFETY_CHECK = "safety_check"


class SafetyFlag(BaseModel):
    """What the safety check decided, kept on the report it raised.

    Copied off the check's ``assess_security`` task when the intervention is
    stored, because nothing else joins the two: the task names the answer it
    assessed by its interview-scoped number, and the intervention message
    deliberately carries no decision -- it is sent to the respondent's client.
    Reviewers are not the respondent, and without the decision and its
    reasoning a flag says only that something happened.
    """

    #: The policy decision that triggered, e.g. ``self_harm``.
    decision: str
    probability: float | None = None
    reasoning: str | None = None
    action: SecurityAction
    respondent_override: bool = False
    #: The respondent's answer in the modal, when they were allowed one and
    #: gave it. NULL until then, and on interventions that offered none.
    override: SecurityOverride | None = None


class ReportStatus(StrEnum):
    """Where a report has got to in one review queue.

    A report carries two of these, one per reviewer -- see
    ``MessageReportTable``. ``DISMISSED`` is distinct from ``RESOLVED``
    because "I have looked at this and there is nothing to do" and "I have
    looked at this and fixed it" are different answers, and collapsing them
    would make the resolved count meaningless.
    """

    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class InterviewType(StrEnum):
    MANUAL_TEST = "manual_test"
    SYNTHETIC_TEST = "synthetic_test"
    DISTRIBUTED = "distributed"


class ContentVersionKind(StrEnum):
    """Why a `projectcontentversion` was written.

    `PUBLISH` and `MANUAL` are saved by someone on purpose and are never
    deleted. The rest are written as a side effect -- `TEST` for the draft a
    test interview ran on, `RESTORE` for the draft a restore replaced, `AUTO`
    for the autosave snapshots of Phase 4 -- and may be pruned once no
    interview points at them (`VersionRepository.prune`).
    """

    PUBLISH = "publish"
    MANUAL = "manual"
    TEST = "test"
    RESTORE = "restore"
    AUTO = "auto"

    @property
    def is_automatic(self) -> bool:
        return self not in (ContentVersionKind.PUBLISH, ContentVersionKind.MANUAL)


class ContentPart(StrEnum):
    """A part of a project's content that is edited, and revisioned, on its
    own: each has its own counter on `projectcontent` (`<part>_revision`), so
    a save to one never conflicts with a save to another. See
    `ContentColumns.PART_FIELDS` for the columns each part covers.
    """

    GUIDE = "guide"
    CONSENT = "consent"
    WELCOME = "welcome"
    #: `agent_configs` and `prompt_overrides`.
    AGENTS = "agents"
    EMAILS = "emails"
    #: `languages`, `default_language` and `languages_in_progress`.
    LANGUAGES = "languages"

    @property
    def revision_column(self) -> str:
        return f"{self.value}_revision"


class VerificationPurpose(StrEnum):
    EMAIL_VERIFICATION = "email_verification"
    LOGIN = "login"
    PASSWORD_RESET = "password_reset"


class EmbeddingTask(StrEnum):
    """Which instruction template a stored vector was produced under.

    Only `DOCUMENT` is ever written today, and it means "no instruction at
    all": the embedding model takes its task on the query side, so one
    document-side vector serves retrieval, classification and reranking alike
    (see `app/embed/templates.py`). The column exists so that a symmetric task
    -- clustering or semantic similarity, where there is no query/document
    split and a task-prefixed variant might measurably help -- can be added as
    a second vector per chunk without a migration, and so that a mixed index is
    never searched by accident.
    """

    DOCUMENT = "document"
    CLUSTERING = "clustering"


class ErrorCode(StrEnum):
    """What went wrong, for a client to branch on: the `code` of an
    `ApiError` body. `detail` is the sentence to show; this is what to test.

    Only errors a client handles differently carry one. Values are part of
    the API -- the frontend matches on them -- so rename none without it.
    """

    INTERNAL = "internal"
    # Content and publishing
    NOT_PUBLISHED = "not_published"
    CANNOT_PUBLISH = "cannot_publish"
    STALE_REVISION = "stale_revision"
    REVISION_REQUIRED = "revision_required"
    PROJECT_LANGUAGE = "project_language"
    # The guide editor: a section added since the last save, which the server
    # does not have yet.
    SECTION_NOT_FOUND = "section_not_found"
    # Interviews
    INVALID_LINK_PARAMS = "invalid_link_params"
    # Analysis
    INVALID_CODEBOOK = "invalid_codebook"
    INVALID_CODING = "invalid_coding"
    INVALID_COMMENT_THREAD = "invalid_comment_thread"
    INVALID_KEYWORD_QUERY = "invalid_keyword_query"
    # Accounts and invitations
    ALIAS_TAKEN = "alias_taken"
    EMAIL_TAKEN = "email_taken"
    EMAIL_NOT_VERIFIED = "email_not_verified"
    EMAIL_SEND_FAILED = "email_send_failed"
    INVITE_REQUIRED = "invite_required"
    INVITE_INVALID = "invite_invalid"
    INVITE_EXPIRED = "invite_expired"
    ACCESS_REQUEST_EXISTS = "access_request_exists"
    PREVIEW_FEATURES_REQUIRED = "preview_features_required"
