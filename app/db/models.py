from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path

from pydantic import (
    UUID4,
    AliasChoices,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    computed_field,
    field_validator,
)

from ainterviewer.agents.config import AgentConfigs
from ainterviewer.config import InterviewConfig
from ainterviewer.interview_guides import Image, InterviewGuide, SurveyItem
from ainterviewer.interview_guides.extra import Consent, Welcome
from ainterviewer.settings import settings as lib_settings
from ainterviewer.synthesize.interviewees import BackgroundInfoOptions, InterviewSubject
from ainterviewer.types import (
    EmbeddingKind,
    Feedback,
    Interviewer,
    InterviewStatus,
    LanguageCode,
    LanguageDict,
    MessageRole,
    MessageType,
    TestType,
    TimeDelta,
)
from ainterviewer.utils import now

from ..settings import app_settings
from ..types import (
    CollaboratorRole,
    ExternalParam,
    GroupKind,
    Projection,
    ProjectStatus,
    Scope,
    TestRunStatus,
    TurnRole,
)
from ._extra import CustomEmailStr
from .types import AccessRequestStatus, AnnotationType, InterviewType


# TODO: Implement across more endpoints
class _Unset(Enum):
    UNSET = "UNSET"


UNSET = _Unset.UNSET


class _BaseModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class AccessRequestBase(_BaseModel):
    name: str
    email: CustomEmailStr
    organization: str | None = None
    message: str | None = None


class AccessRequestCreate(AccessRequestBase): ...


class AccessRequestPublic(AccessRequestBase):
    id: UUID4
    created_at: datetime
    updated_at: datetime
    status: AccessRequestStatus
    processed_by_id: UUID4 | None


class InvitationBase(_BaseModel):
    expires_at: datetime | None = None
    reuseable: bool = False
    user_scope: Scope = Scope.USER
    user_expires: datetime | TimeDelta | None = None
    title: str | None = None
    access_request_id: UUID4 | None = None


class InvitationCreate(InvitationBase): ...


class InvitationUpdate(BaseModel):
    email: str | None | _Unset = UNSET
    expires_at: datetime | None | _Unset = UNSET
    reuseable: bool | _Unset = UNSET
    user_scope: Scope | _Unset = UNSET
    user_expires: datetime | TimeDelta | None | _Unset = UNSET
    title: str | None | _Unset = UNSET


class InvitationPublic(InvitationBase):
    id: UUID4
    email: str | None

    @computed_field()
    def invitation_link(self) -> str:
        return f"{app_settings.sveltekit_platform_public_addr}/sign-up?token={self.id}"


class UserBase(_BaseModel):
    email: EmailStr
    first_name: str
    last_name: str | None = None
    created_at: datetime
    last_active: datetime
    last_login: datetime
    scope: Scope = Scope.USER


class UserCreateRequest(UserBase):
    """API request model for user registration."""

    invite_token: UUID4 | str | None = Field(union_mode="left_to_right")

    created_at: datetime = Field(default_factory=now)
    last_active: datetime = Field(default_factory=now)
    last_login: datetime = Field(default_factory=now)
    research_consent: bool = False
    password: str = Field(min_length=8)

    @field_validator("password")
    @classmethod
    def _password_within_bcrypt_limit(cls, value: str) -> str:
        # bcrypt silently truncates input beyond 72 bytes (not chars), so
        # reject anything longer to avoid surprising password equivalence.
        if len(value.encode("utf-8")) > 72:
            raise ValueError("Password must be at most 72 bytes")
        return value


class UserCreate(UserCreateRequest):
    """Internal model for creating a user, includes snapshot fields."""

    registration_token: str | None = None
    invitation_title: str | None = None
    expires_at: datetime | None = None
    access_request_message: str | None = None
    organization: str | None = None


class UserPrivate(UserBase):
    id: UUID4
    password: str
    with_demo_features: bool
    organization: str | None = None
    email_verified: bool = False
    two_factor_enabled: bool = True


class UserPublic(UserBase):
    id: UUID4
    invitation_title: str | None = None
    expires_at: datetime | None = None
    with_demo_features: bool
    organization: str | None = None


class UserAdmin(UserPublic):
    access_request_message: str | None = None
    admin_note: str | None = None
    admin_note_updated_at: datetime | None = None
    two_factor_enabled: bool = True


class UserAdminUpdate(BaseModel):
    scope: Scope | _Unset = UNSET
    with_demo_features: bool | _Unset = UNSET
    organization: str | None | _Unset = UNSET
    expires_at: datetime | None | _Unset = UNSET
    two_factor_enabled: bool | _Unset = UNSET


class UserSelfUpdate(BaseModel):
    """Profile fields a user may change on their own account."""

    first_name: str | _Unset = UNSET
    last_name: str | None | _Unset = UNSET
    organization: str | None | _Unset = UNSET


class Collaborator(_BaseModel):
    email: EmailStr
    role: CollaboratorRole


class CollaboratorBase(_BaseModel):
    role: CollaboratorRole


class CollaboratorCreate(CollaboratorBase):
    email: EmailStr


class CollaboratorPublic(CollaboratorBase):
    id: UUID4
    user: UserPublic
    added_at: datetime


class ProjectLocalizationBase(_BaseModel):
    project_id: UUID4
    language: LanguageCode
    consent: Consent | None
    welcome: Welcome | None
    interview_guide: InterviewGuide
    prompt_overrides: dict[str, str]
    agent_configs: AgentConfigs
    created_at: datetime
    last_updated: datetime | None = None


class ProjectLocalizationCreate(_BaseModel):
    language: LanguageCode
    interview_guide: InterviewGuide | None = None
    prompt_overrides: dict[str, str] | None = None
    agent_configs: AgentConfigs | None = None


class ProjectLocalizationPublic(ProjectLocalizationBase):
    id: UUID4


class ProjectFolderBase(_BaseModel):
    title: str


class ProjectFolderCreate(ProjectFolderBase):
    collaborators: list[Collaborator] = Field(
        default=[], validation_alias="folder_collaborations"
    )


class ProjectFolderPublic(ProjectFolderBase):
    id: UUID4
    collaborators: list[CollaboratorPublic] = Field(
        default=[], validation_alias="folder_collaborations"
    )


class ProjectFolderEdit(ProjectFolderBase): ...


class ProjectFolderWithProjects(ProjectFolderPublic):
    projects: list[ProjectPublic]


class ProjectBase(_BaseModel):
    model_config = {"extra": "forbid", "use_enum_values": True}

    id: UUID4
    title: str
    created_at: datetime
    last_updated: datetime | None = None
    status: ProjectStatus = ProjectStatus.ACTIVE
    config: InterviewConfig
    external_params: list[ExternalParam] | None = None
    owner_id: UUID4


class ProjectCreate(_BaseModel):
    title: str
    config: InterviewConfig | None = None


class ProjectLanguage(LanguageDict):
    """A language a project has a localization for.

    Same shape as the library's `LanguageDict` plus whether it is the
    project's default. `LanguageDict` itself stays free of the flag: it comes
    straight out of the shared `LANGUAGES` constant and knows nothing about
    projects.
    """

    is_default: bool


class ProjectPublic(ProjectBase):
    n_interviews: int | None = None
    available_languages: list[ProjectLanguage] | None = None
    tests: list[TestSetupPublic] | None = None
    owner: UserPublic


class ProjectPermissionsPublic(_BaseModel):
    """What the caller may do in one project.

    The UI asks for this so it can leave out the actions the API would refuse,
    such as editing somebody else's comment. It is a convenience, never the
    check itself: every endpoint still enforces its own rights.
    """

    role: CollaboratorRole | None = None
    is_owner: bool
    can_moderate: bool


class ProjectPublicWithTests(ProjectPublic):
    tests: list[TestSetupPublic]


class ExperimentProjectCreate(_BaseModel):
    """Input model for adding a project to an experiment."""

    project_id: UUID4
    weight: float | None = None


class ExperimentProjectPublic(_BaseModel):
    """Public model for experiment-project association."""

    id: UUID4
    project_id: UUID4
    weight: float | None = None
    added_at: datetime


class ExperimentCreate(_BaseModel):
    title: str
    projects: list[ExperimentProjectCreate]


class ExperimentPublic(_BaseModel):
    id: UUID4
    title: str
    user_id: UUID4
    created_at: datetime
    status: ProjectStatus = ProjectStatus.ACTIVE
    projects: list[ExperimentProjectPublic] = []


class InterviewBase(_BaseModel):
    id: UUID4
    interview_guide: InterviewGuide | None
    language: LanguageCode = "EN"
    interviewer: Interviewer = Interviewer.AI
    status: InterviewStatus = InterviewStatus.INACTIVE
    type: InterviewType = InterviewType.DISTRIBUTED
    created_at: datetime
    last_updated: datetime | None = None
    total_time_spent: int = 0
    survey_token: str | None = None
    user_agent: str | None = None
    ip_address: str | None = None
    referer: str | None = None
    platform_version: str | None = None
    test_name: str | None = None


class InterviewCreate(_BaseModel):
    interview_guide: InterviewGuide
    language: LanguageCode = "EN"
    interviewer: Interviewer = Interviewer.AI
    project_id: UUID4
    experiment_id: UUID4 | None = None


class InterviewPublic(InterviewBase):
    # Both default, so an interview can be returned without its transcript.
    # `n_messages` comes from the SQL expression on InterviewTable rather than
    # from `len(messages)`, so it stays correct when the messages are not
    # loaded -- see InterviewRepository.get_interview.
    n_messages: int = 0
    messages: list[MessagePublic] = []


class InterviewSummaryPublic(_BaseModel):
    """One row of the interview list. Deliberately carries no messages: the
    list only ever renders `n_messages`, and the transcript is fetched from
    /interviews/{id}/messages when a single interview is opened."""

    id: UUID4
    language: LanguageCode = "EN"
    interviewer: Interviewer = Interviewer.AI
    status: InterviewStatus
    type: InterviewType
    created_at: datetime
    last_updated: datetime | None = None
    total_time_spent: int = 0
    n_messages: int
    test_name: str | None = None
    # The participant this interview belongs to, joined in from the
    # participant record. NULL for interviews that were never distributed to
    # one, and for interviews whose participant has since been deleted.
    pid: str | None = None


class MessageBase(_BaseModel):
    message_id: int
    content: str
    role: MessageRole
    interview_id: UUID4
    project_id: UUID4
    message_type: MessageType = MessageType.TEXT
    section: int | None = None
    main_question: int | None = None
    sub_question: int | None = None
    is_introduction: bool = False
    outro: bool = False
    timed: bool = False
    can_answer: bool = True
    include_in_history: bool = True
    attachment: Path | None = None
    audio_file: str | None = None
    feedback: Feedback | None = None
    created_at: datetime
    image: Image | list[Image] | None = None
    survey_item: SurveyItem | None = None
    skipped_by_condition: bool = False


class MessageCreate(_BaseModel):
    message_id: int
    content: str
    role: MessageRole
    interview_id: UUID4
    project_id: UUID4
    message_type: MessageType = MessageType.TEXT
    section: int | None = None
    main_question: int | None = None
    sub_question: int | None = None
    is_introduction: bool = False
    outro: bool = False
    timed: bool = False
    can_answer: bool = True
    include_in_history: bool = True
    attachment: Path | None = None
    audio_file: str | None = None
    feedback: Feedback | None = None
    image: Image | list[Image] | None = None
    survey_item: SurveyItem | None = None
    skipped_by_condition: bool = False


class MessagePublic(MessageBase):
    id: UUID4
    annotations: list[MessageAnnotationPublic] = []
    comments: list[MessageCommentPublic] = []

    @field_validator("comments", mode="after")
    @classmethod
    def _roots_only(
        cls, comments: list[MessageCommentPublic]
    ) -> list[MessageCommentPublic]:
        """The ORM relationship holds every comment on the message; the thread
        is exposed as roots carrying their own replies."""
        return [comment for comment in comments if comment.parent_id is None]

    interview_type: InterviewType


class TaskBase(_BaseModel):
    id: UUID4
    created_at: datetime
    message_id: int
    interview_id: UUID4
    project_id: UUID4
    task: str
    reason: str | None = None
    content: str | None = None
    response: str | None = None
    model: str | None = None
    time_spend: int | None = None


class TaskCreate(_BaseModel):
    message_id: int
    interview_id: UUID4
    project_id: UUID4
    task: str
    reason: str | None = None
    content: str | None = None
    response: str | None = None
    model: str | None = None
    time_spend: int | None = None


class TaskPublic(TaskBase):
    pass


class TestSetupBase(_BaseModel):
    name: str | None = None
    type: TestType
    project_id: UUID4
    answering_model: str = lib_settings.llm.default_model
    last_updated: datetime | None = None
    language: LanguageCode = "EN"
    n_interviews: int = 5
    delay_before_answers: tuple[float, float] | None = None


class TestSetupCreate(TestSetupBase):
    pass


class TestSetupPublic(TestSetupBase):
    n_runs: int
    id: UUID4
    created_at: datetime
    background_info: BackgroundInfoOptions | None = None
    fixed_answers: list[str] | None = None
    fixed_personas: list[str] | None = None


class TestRunBase(_BaseModel):
    test_setup_id: UUID4
    language: LanguageCode = "EN"
    n_interviews: int
    answering_model: str
    delay_before_answers: tuple[float, float] | None = None


class TestRunCreate(TestRunBase):
    pass


class TestRunPublic(TestRunBase):
    id: UUID4
    created_at: datetime
    last_updated: datetime | None = None
    status: TestRunStatus


class ParticipantBase(_BaseModel):
    name: str | None = None
    email: EmailStr | None = None
    pid: str | None = None
    participating: bool = True
    lang: LanguageCode | None = None


class ParticipantCreate(ParticipantBase):
    pass


class ParticipantUpdate(BaseModel):
    name: str | None | _Unset = UNSET
    email: EmailStr | None | _Unset = UNSET
    pid: str | _Unset = UNSET
    participating: bool | _Unset = UNSET


class ParticipantPublic(ParticipantBase):
    id: UUID4
    project_id: UUID4
    participant_id: UUID4
    folder_id: UUID4
    created_at: datetime
    pid: str
    latest_interview_at: datetime | None = None
    latest_interview_status: InterviewStatus | None = None


class IntervieweeBase(_BaseModel):
    interview_id: UUID4
    interview_subject: InterviewSubject | str


class IntervieweeCreate(IntervieweeBase):
    pass


class IntervieweePublic(IntervieweeBase):
    id: UUID4


############
# Analysis #
############


class AnalysisCategoryBase(_BaseModel):
    project_id: UUID4
    name: str
    description: str | None = None
    type: AnnotationType
    color: str
    min_value: int | None = None
    max_value: int | None = None


class AnalysisCategoryCreate(AnalysisCategoryBase):
    pass


class AnalysisCategoryPublic(AnalysisCategoryBase):
    id: UUID4
    created_at: datetime


class FilteredMessagesRequest(_BaseModel):
    category_ids: list[UUID4] | None = None
    search_text: str | None = None
    exact_match: bool = False
    case_sensitive: bool = False
    questions: list[tuple[int, int]] | None = None
    include_previous_on_user: bool = True


class AnnotationValueBase(_BaseModel):
    category_id: UUID4
    value_int: int


class AnnotationValueCreate(AnnotationValueBase):
    pass


class AnnotationValuePublic(AnnotationValueBase):
    id: UUID4


class AuthorPublic(_BaseModel):
    """Who wrote an annotation or a comment.

    Annotations and comments are author specific, so every one of them is shown
    with a name attached. Carrying the author inline saves the client from
    resolving user ids against a separate collaborator listing.
    """

    id: UUID4
    first_name: str
    last_name: str | None = None
    email: EmailStr


class MessageAnnotationBase(_BaseModel):
    message_id: UUID4
    user_id: UUID4


class MessageAnnotationCreate(MessageAnnotationBase):
    values: list[AnnotationValueCreate]


class MessageAnnotationPublic(MessageAnnotationBase):
    id: UUID4
    created_at: datetime
    updated_at: datetime
    values: list[AnnotationValuePublic]
    # The ORM relationship is called ``user``; the payload calls it ``author``.
    author: AuthorPublic = Field(validation_alias=AliasChoices("author", "user"))


class MessageCommentCreate(_BaseModel):
    """A new comment. The author is taken from the caller's token, never from
    the payload; ``parent_id`` must name a root comment on the same message."""

    body: str = Field(min_length=1)
    parent_id: UUID4 | None = None


class MessageCommentUpdate(_BaseModel):
    body: str = Field(min_length=1)


class MessageCommentPublic(_BaseModel):
    id: UUID4
    message_id: UUID4
    user_id: UUID4
    parent_id: UUID4 | None
    body: str
    created_at: datetime
    updated_at: datetime
    # The ORM relationship is called ``user``; the payload calls it ``author``.
    author: AuthorPublic = Field(validation_alias=AliasChoices("author", "user"))
    # Only ever populated on a root comment: threads are two levels deep.
    replies: list[MessageCommentPublic] = []


##############
# Embeddings #
##############


class EmbeddingTurn(_BaseModel):
    """One speaker turn inside a chunk, as it was said in the interview.

    A chunk's stored `text` is the rendering the *model* saw -- one string with
    ``Q:``/``A:`` prefixes -- and re-splitting it on those prefixes would be a
    parse of prose that a respondent can break by starting a sentence with
    "Q:". These come from the message rows instead, so the roles are structural
    and a result reads the way the conversation did.
    """

    role: TurnRole
    text: str
    # The survey item type, when the answer was a chosen option rather than
    # written text. The distinction matters to a reader: an identical "Agree"
    # from forty respondents is a click, not a consensus.
    survey_label: str | None = None
    # The one turn a MESSAGE hit is actually about; its neighbours are context.
    match: bool = False


class EmbeddingSearchHit(_BaseModel):
    """One semantic-search result, renderable on its own.

    Carries the matched text and the interview context around it, because the
    alternative is a request per hit: a QA-pair chunk spans several messages and
    has no `message_id` to fetch, so there is nothing a client could resolve it
    to. `message_id` is set for MESSAGE hits only, and is the handle for the
    existing annotation, comment and message-context endpoints.
    """

    id: UUID4
    # Cosine similarity in [-1, 1]; vectors are L2-normalised, so this is a
    # plain dot product. Exposed so a client can show ranking confidence and
    # cut off weak matches, which a bare ordering cannot support.
    score: float
    kind: EmbeddingKind
    text: str | None

    interview_id: UUID4
    message_id: UUID4 | None
    section: int | None
    main_question: int | None
    sub_question: int | None
    language: LanguageCode

    # Interview context, so a result row can say when and from whom without a
    # follow-up request.
    interview_created_at: datetime | None = None
    interview_status: InterviewStatus | None = None
    interview_type: InterviewType | None = None
    participant_id: UUID4 | None = None
    participant_pid: str | None = None

    # The chunk as a conversation, when the messages behind it could be found.
    # Empty is a normal state, not an error -- an interview whose message rows
    # no longer line up with the chunk's coordinates still has its `text`, and
    # a client renders that instead.
    turns: list[EmbeddingTurn] = []

    @classmethod
    def from_hit(
        cls,
        embedding,
        score: float,
        turns: list[EmbeddingTurn] | None = None,
    ) -> EmbeddingSearchHit:
        interview = embedding.interview
        project_participant = interview.project_participant if interview else None
        participant = project_participant.participant if project_participant else None

        return cls(
            id=embedding.id,
            score=score,
            kind=embedding.kind,
            text=embedding.text,
            interview_id=embedding.interview_id,
            message_id=embedding.message_id,
            section=embedding.section,
            main_question=embedding.main_question,
            sub_question=embedding.sub_question,
            language=embedding.language,
            interview_created_at=interview.created_at if interview else None,
            interview_status=interview.status if interview else None,
            interview_type=interview.type if interview else None,
            participant_id=project_participant.id if project_participant else None,
            participant_pid=participant.pid if participant else None,
            turns=turns or [],
        )


class EmbeddingSearchResponse(_BaseModel):
    """Top-k results for one query.

    Not paginated: k is chosen up front and the whole point of a ranked search
    is that results past the cut-off are not worth a page.
    """

    query: str
    kind: EmbeddingKind
    task: str
    # How many chunks the query was scored against after filtering. Lets a
    # client tell "nothing matched" from "the filters left nothing to match".
    candidates: int = 0
    items: list[EmbeddingSearchHit] = []


class EmbeddingSimilarResponse(_BaseModel):
    """Neighbours of a chunk already in the corpus."""

    source: EmbeddingSearchHit
    candidates: int = 0
    items: list[EmbeddingSearchHit] = []


class EmbeddingStatus(_BaseModel):
    """Whether a project's corpus is embedded, and whether it could be."""

    enabled: bool
    healthy: bool
    model: str
    dimension: int
    # Stored vectors per `EmbeddingKind`, for this project.
    coverage: dict[str, int] = {}
    # Stored vectors per language code, most-embedded first. Reported here so a
    # client can offer a language filter whose options are the project's, not
    # whatever the last query happened to return.
    languages: dict[LanguageCode, int] = {}
    total: int = 0
    # Live queue, process-wide rather than per project: chunks waiting to be
    # embedded, and chunks dropped because the queue was full since startup.
    # A non-zero drop count is not data loss -- the backfill re-derives them --
    # but it does mean search results are behind.
    queue_depth: int = 0
    queue_dropped: int = 0


class EmbeddingBackfillResponse(_BaseModel):
    """What one backfill trigger put in flight."""

    queued: int
    # Chunks that needed embedding but did not fit in the queue. They are not
    # lost -- the next trigger or a CLI run picks them up -- but they are not
    # coming in this round either.
    skipped: int
    # Interviews whose stored history could not be reconstructed, usually a
    # guide snapshot that no longer matches the messages recorded against it.
    failed_interviews: list[str] = []
    queue_depth: int


class EmbeddingClusterPoint(_BaseModel):
    """One chunk's position in the scatter plot."""

    id: UUID4
    # None means HDBSCAN declined to place it. Outliers are kept rather than
    # dropped: in interview data the unplaceable answers are often the ones
    # worth reading.
    cluster: int | None
    # HDBSCAN's confidence that the point belongs to its cluster, 0 for outliers.
    probability: float
    x: float
    y: float
    preview: str | None = None
    # The interview-guide coordinates the chunk came from, so the same scatter
    # can be coloured by what the guide asked rather than by what clustering
    # found. NULL on an interview chunk, which spans the whole guide.
    section: int | None = None
    main_question: int | None = None
    sub_question: int | None = None
    # The interview's language. Sent for the same reason as the guide
    # coordinates: on a multilingual project the model separates languages
    # before it separates topics, and colouring by this is how that becomes
    # visible instead of being mistaken for two themes.
    language: LanguageCode


class EmbeddingCluster(_BaseModel):
    id: int
    size: int
    # Members nearest the cluster centre, in full: the material for naming it.
    representatives: list[EmbeddingSearchHit] = []
    # Share of members from the single most common interview question. Near 1.0
    # means the cluster is really just that question -- a QA-pair chunk repeats
    # its question verbatim for every respondent, so uncentred clustering tends
    # to recover the interview guide. Read this before reading the clusters.
    question_purity: float | None = None
    # Share of members from the single most common language. Near 1.0 on a
    # project that ran in more than one means the cluster is a language: the
    # model separates Danish from English more strongly than it separates
    # anything either of them says. Always 1.0 when only one language is in
    # scope, where it means nothing -- read it against `groups`.
    language_purity: float | None = None


class EmbeddingGroup(_BaseModel):
    """A named set of points to colour the scatter by.

    Questions and sections come from the guide rather than from the data, so
    unlike a cluster they arrive already named -- which is what makes them the
    baseline worth reading the clusters against.
    """

    kind: GroupKind
    # `"<section>"` for a section, `"<section>.<main_question>"` for a question.
    # A string because it is a compound key and clients use it as a dictionary
    # key, not as a number.
    key: str
    # As the guide numbers it: "Q2.1" or "Section 2".
    label: str
    # The guide's own wording, when the question still exists in the current
    # draft. NULL for a question an interview asked under an older snapshot.
    text: str | None = None
    size: int


class EmbeddingClusterResponse(_BaseModel):
    kind: EmbeddingKind
    n_points: int
    n_clusters: int
    n_outliers: int
    # How the vectors were reduced. Under "pca" the scatter is a linear
    # projection, so distances on it are comparable everywhere; under "umap"
    # only adjacency is meaningful -- who sits next to whom, never how far
    # apart two clusters are or how big one looks.
    projection: Projection
    # Dimensions clustering ran in. The scatter shows the first two of them,
    # and under "umap" there are only those two.
    components: int
    # Share of total variance the two plotted axes carry. Typically low for text
    # embeddings: the plot is a navigation aid, not evidence. Points far apart
    # on screen are genuinely far apart; points close together may not be.
    # NULL under "umap", which has no such quantity.
    explained_variance_2d: float | None = None
    centered_by_question: bool
    # Whether each language's mean vector was subtracted before projection. On a
    # multilingual project this is what stops the biggest clusters from simply
    # being the languages.
    centered_by_language: bool = False
    clusters: list[EmbeddingCluster] = []
    # Every guide group the plotted points fall into, questions and sections
    # alike, ordered as the guide orders them. Sent alongside the clusters
    # rather than behind a parameter: it is derived from the same points, costs
    # one guide read, and lets a client switch grouping without a round trip.
    groups: list[EmbeddingGroup] = []
    points: list[EmbeddingClusterPoint] = []
