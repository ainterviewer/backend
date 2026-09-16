from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Literal
from uuid import UUID

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
from .types import AccessRequestStatus, CodeKind, InterviewType


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
    codings: list[CodingPublic] = []
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


class CodeBase(_BaseModel):
    """One code as the client sends it, id and all.

    The id is the client's because a code is dragged, renamed and coded with
    long before the codebook is saved; see ``CodeTable``.
    """

    id: UUID4
    parent_id: UUID4 | None = None
    name: str = ""
    definition: str = ""
    memo: str = ""
    color: str = ""
    kind: CodeKind = CodeKind.TAG
    min_value: int | None = None
    max_value: int | None = None
    position_x: float | None = None
    position_y: float | None = None


class CodePublic(CodeBase):
    created_at: datetime
    updated_at: datetime


class CodebookPut(_BaseModel):
    """A whole codebook, replacing the stored one.

    The codebook is edited as one document -- a drag re-parents a branch and
    reorders two sets of siblings at once, and the editor holds an undo stack
    over the whole thing -- so it is saved as one, and ``codes`` is authoritative:
    a code the client leaves out is deleted, along with every coding made with
    it. List order is sibling order.
    """

    codes: list[CodeBase]
    palette: list[str]


class CodebookPublic(_BaseModel):
    """The stored codebook, in the order the tree reads."""

    codes: list[CodePublic]
    palette: list[str]


class FilteredMessagesRequest(_BaseModel):
    code_ids: list[UUID4] | None = None
    search_text: str | None = None
    exact_match: bool = False
    case_sensitive: bool = False
    questions: list[tuple[int, int]] | None = None
    include_previous_on_user: bool = True


class AuthorPublic(_BaseModel):
    """Who wrote a coding or a comment.

    Codings and comments are author specific, so every one of them is shown
    with a name attached. Carrying the author inline saves the client from
    resolving user ids against a separate collaborator listing.
    """

    id: UUID4
    first_name: str
    last_name: str | None = None
    email: EmailStr


#: How many messages one codings lookup may name. A page of explore results is
#: tens of turns; this is room for several pages and a bound on the query.
MAX_CODING_LOOKUP = 500


class CodingBase(_BaseModel):
    """One passage coded with one code.

    ``start_offset``/``end_offset`` are character offsets into the message's
    content, or both NULL for the whole message. ``value_int`` carries a
    score's number and is NULL on a tag.
    """

    code_id: UUID4
    start_offset: int | None = None
    end_offset: int | None = None
    value_int: int | None = None


class CodingCreate(CodingBase):
    pass


class CodingsForMessages(_BaseModel):
    """Which messages to read the codings of.

    A POST for a read, like the filtered-message endpoints beside it: the
    explore page draws a page of results as a mosaic of turns from many
    interviews, so the ask is a few hundred message ids -- more than belongs in
    a query string, and a request per turn would be a request per turn.
    """

    message_ids: list[UUID4] = Field(max_length=MAX_CODING_LOOKUP)


class CodingPublic(CodingBase):
    id: UUID4
    message_id: UUID4
    user_id: UUID4
    created_at: datetime
    updated_at: datetime
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

    #: The message row this turn is, so a client can act on it.
    #:
    #: On the base model rather than only on `TranscriptTurn` because a turn is
    #: the unit a reader codes, and explore draws turns everywhere -- inside a
    #: QA-pair or section card as much as in a transcript. A hit's own
    #: `message_id` is set for MESSAGE chunks alone, so without this there is
    #: nothing to hang a coding on anywhere else, and coding from the results
    #: list would be possible at one unit out of four.
    id: UUID
    role: TurnRole
    text: str
    # The survey item type, when the answer was a chosen option rather than
    # written text. The distinction matters to a reader: an identical "Agree"
    # from forty respondents is a click, not a consensus.
    survey_label: str | None = None
    # The one turn a MESSAGE hit is actually about; its neighbours are context.
    match: bool = False
    # Where in `text` the keyword query matched, as `(start, end)` character
    # offsets into this string, already merged and in order.
    #
    # Reported rather than left to the client because the scope decides it: with
    # the search set to answers, the same word in the interviewer's question is
    # not a match, and only the side that ran the query knows that. Offsets and
    # not marked-up text -- the client escapes what it renders.
    matches: list[tuple[int, int]] = Field(default_factory=list)
    # Where in `text` a term the query *excluded* appears anyway, same offsets.
    #
    # A negated term can survive on screen two ways: in text the scope never
    # searched, or in a sibling turn of a grouped chunk, since the condition is
    # checked per message and then lifted to the group. Reported separately so a
    # reader can see why a result looks like it contradicts its own query.
    excluded: list[tuple[int, int]] = Field(default_factory=list)
    # Where in the guide this turn was said, so a message can be numbered the
    # way the transcript numbers it -- `3.2` for a main question, `3.2.1` for
    # the first probe under it.
    #
    # Carried per turn rather than read off the chunk, even though a chunk's
    # turns all sit under one question by construction: the probes inside a
    # question group are exactly what differ, and a card that stamped the
    # group's number on all of them would say `3.2` three times where the
    # transcript says `3.2.1`, `3.2.2`, `3.2.3`. An INTERVIEW chunk spans the
    # whole guide and has no number of its own at all.
    section: int | None = None
    main_question: int | None = None
    sub_question: int | None = None


class TranscriptTurn(EmbeddingTurn):
    """One turn of a whole interview, for reading a hit in its context.

    An `EmbeddingTurn` with everything a chunk has no room for: the survey item
    in full, the image, and whether the guide skipped past it. The message id
    and the coordinates it is scrolled to are the base model's, since a card
    identifies and numbers its turns from them too.

    Inherits `matches`/`excluded` rather than restating them, so a transcript
    renders through the same component a chunk does -- the search is still
    marked once the reader has left the mosaic, which is most of the point of
    reading the transcript at all.
    """

    #: The survey item in full, where the answer was a chosen option.
    #:
    #: `EmbeddingTurn.survey_label` carries only the item's *type*, which is all
    #: a results card has room for. A transcript is read to judge an answer, and
    #: an option cannot be judged apart from the options it was chosen from --
    #: "Rarely" means nothing until you can see it was picked over "Never".
    #:
    #: Carried on the answer rather than on the question that posed it, the same
    #: move the transcript page makes: the item belongs to the interviewer's
    #: message in the database and to the respondent's on screen.
    survey_item: SurveyItem | None = None
    #: Asked but never reached, because a condition routed around it. Kept
    #: rather than dropped: a question the guide skipped is part of how the
    #: interview went, and the transcript page has always shown it faded.
    skipped: bool = False
    #: The image attached to the message, where there was one.
    image: Image | None = None


class InterviewTranscript(_BaseModel):
    """A whole interview as turns, marked with what the keyword query found.

    Separate from the messages endpoint the transcript *page* loads, which
    serves annotations and comments and knows nothing about a keyword query.
    This one exists so the explore view can open a transcript without a
    navigation, and it answers only that: read it, see the search in it, go
    back. Annotating is still the page's.
    """

    interview_id: UUID
    turns: list[TranscriptTurn]


#: How much of an interview-level chunk a result card is given.
#:
#: An INTERVIEW chunk spans a whole transcript, and a page of ten of them
#: rendered whole is not a list anybody can scan -- it is ten transcripts, and
#: the reader already has a transcript view a click away. So the card gets a
#: window onto the conversation rather than the conversation: enough turns to
#: hear what kind of interview this is, with `ChunkTurns.total` saying how much
#: was left behind so the card can be honest about being a window.
#:
#: Six is three exchanges, which is where an interview stops sounding like its
#: opening pleasantries and starts sounding like itself.
INTERVIEW_PREVIEW_TURNS = 6


@dataclass(frozen=True)
class ChunkTurns:
    """The turns a card draws for one chunk, and how many the chunk holds.

    The two are the same number for every kind but INTERVIEW, which is windowed
    -- see `INTERVIEW_PREVIEW_TURNS`. Returned as a pair rather than as a bare
    list so that a truncated card can say so: "6 of 47 turns" is a window, while
    six turns with no count is a claim that the interview was six turns long.
    """

    turns: list[EmbeddingTurn]
    total: int


class EmbeddingSearchHit(_BaseModel):
    """One semantic-search result, renderable on its own.

    Carries the matched text and the interview context around it, because the
    alternative is a request per hit: a QA-pair chunk spans several messages and
    has no `message_id` to fetch, so there is nothing a client could resolve it
    to. `message_id` is set for MESSAGE hits only, and is the handle for the
    existing annotation, comment and message-context endpoints.
    """

    # `UUID` rather than `UUID4`: a browsed unit that was never embedded has no
    # row of its own, so its id is derived from its coordinates with `uuid5` --
    # deterministic, so the same chunk keeps the same id across processes, and
    # therefore version 5. Demanding version 4 here rejected exactly the rows
    # this endpoint exists to serve.
    id: UUID
    # Cosine similarity in [-1, 1]; vectors are L2-normalised, so this is a
    # plain dot product. Exposed so a client can show ranking confidence and
    # cut off weak matches, which a bare ordering cannot support.
    #
    # None on a browsed row, which was not ranked against anything: a browse is
    # the corpus in guide order, and inventing a score for it would invite a
    # reader to compare numbers that mean nothing.
    score: float | None = None
    # Whether this unit has a stored vector. False only when browsing a corpus
    # that was never embedded -- the row still reads, but there is nothing to
    # ask it what it is near, so a client hides "more like this" rather than
    # offering a button that 404s.
    embedded: bool = True
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
    # Which interview this is within its project, counting from one in the
    # order they were started.
    #
    # A number rather than the id, because the reader's question is "are these
    # two cards the same person" and a UUID cannot be held in the eye long
    # enough to answer it. Numbered over the whole project rather than over the
    # result set, so it means the same thing in every search, in the transcript
    # it opens, and tomorrow -- a number that renumbered itself per query would
    # be worse than none, since it would look like it identified something.
    #
    # Not a substitute for `participant_pid`: a pid identifies a person across
    # projects and is often absent, while this identifies an interview within
    # one project and is always there.
    interview_number: int | None = None

    # The chunk as a conversation, when the messages behind it could be found.
    # Empty is a normal state, not an error -- an interview whose message rows
    # no longer line up with the chunk's coordinates still has its `text`, and
    # a client renders that instead.
    turns: list[EmbeddingTurn] = []

    # How many turns the unit actually holds, when its messages could be found.
    #
    # The same as `len(turns)` for every kind but INTERVIEW, whose card gets a
    # window onto the transcript rather than the transcript. A client draws the
    # difference -- "6 of 47 turns" -- so that a windowed card reads as a window
    # and not as a short interview. None where there were no messages to count,
    # which is the same state as an empty `turns`.
    n_turns: int | None = None

    @classmethod
    def from_hit(
        cls,
        embedding,
        score: float | None,
        turns: ChunkTurns | None = None,
        number: int | None = None,
    ) -> EmbeddingSearchHit:
        """One row, from either an embedding or a browsed unit.

        `BrowseUnit` is shaped to answer the same attributes deliberately, so
        browsing renders through this rather than through a parallel model that
        would drift from it card by card.
        """
        interview = embedding.interview
        project_participant = interview.project_participant if interview else None
        participant = project_participant.participant if project_participant else None

        return cls(
            id=embedding.id,
            score=score,
            embedded=getattr(embedding, "embedded", True),
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
            interview_number=number,
            turns=turns.turns if turns else [],
            n_turns=turns.total if turns else None,
        )


class EmbeddingSearchResponse(_BaseModel):
    """One page of results for one query.

    Paged with `limit`/`offset` like the rest of the dashboard's lists, but
    `total` is not a promise that every row is worth reading: the tail of a
    ranked scan is whatever scored least, not a further set of matches. The
    scores are in the response so a client can cut its own cut-off.
    """

    query: str
    kind: EmbeddingKind
    task: str
    # How many chunks the query was scored against after filtering. Lets a
    # client tell "nothing matched" from "the filters left nothing to match".
    candidates: int = 0
    # How many of those could be returned at all, i.e. the length of the
    # ranking `offset` walks. Equal to `candidates` here; one fewer on
    # `/similar`, which never returns its own source.
    total: int = 0
    # How many distinct interviews the counted chunks come from.
    #
    # Counted over everything that matched rather than over the page, which is
    # the only version that holds still: a client counting the interviews in
    # what it has loaded would show a number that climbs with every "Load
    # more", and a reader would read that as the corpus growing.
    interviews: int = 0
    offset: int = 0
    items: list[EmbeddingSearchHit] = []


class EmbeddingSimilarResponse(_BaseModel):
    """One page of the neighbours of a chunk already in the corpus."""

    source: EmbeddingSearchHit
    candidates: int = 0
    total: int = 0
    # How many distinct interviews the counted chunks come from.
    #
    # Counted over everything that matched rather than over the page, which is
    # the only version that holds still: a client counting the interviews in
    # what it has loaded would show a number that climbs with every "Load
    # more", and a reader would read that as the corpus growing.
    interviews: int = 0
    offset: int = 0
    items: list[EmbeddingSearchHit] = []


class EmbeddingBrowseResponse(_BaseModel):
    """One page of the corpus with no query behind it.

    The list view's resting state: the filters alone decide what is in it, and
    guide order decides the sequence. `total` here *is* a count of things worth
    reading, unlike the ranked endpoints -- nothing was scored, so nothing is
    tailing off.
    """

    kind: EmbeddingKind
    total: int = 0
    # How many distinct interviews the counted chunks come from.
    #
    # Counted over everything that matched rather than over the page, which is
    # the only version that holds still: a client counting the interviews in
    # what it has loaded would show a number that climbs with every "Load
    # more", and a reader would read that as the corpus growing.
    interviews: int = 0
    offset: int = 0
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


class SurveyFacetValue(_BaseModel):
    """One answer on offer in the survey filter, with how many gave it."""

    #: Position in the item's option list, or None for a write-in. What the
    #: filter is expressed in: the same option is the same position in every
    #: language the item was asked in, and its text is not.
    option: int | None = None
    #: What to show for it. The authored wording where the guide still has the
    #: item, the respondent's own where it does not.
    label: str
    #: Interviews whose respondent gave this answer. Interviews rather than
    #: answers because that is what the filter selects -- picking a value with
    #: `count` beside it should not be able to return chunks from more
    #: interviews than it named.
    count: int


class SurveyFacet(_BaseModel):
    """One survey item, as something to filter a cohort by."""

    section: int
    main_question: int
    #: The question as authored in the project's default localization, falling
    #: back to the wording an interview actually asked.
    question: str
    #: The survey item's own type -- `radio`, `slider`, `date` and so on.
    type: str
    #: How it is filtered: `values` for a checklist of options, `range` for the
    #: ordered ones. Derived from the type here rather than in the client, so
    #: the two cannot disagree about what a control to draw for a `likert` is.
    filter: Literal["values", "range"]
    #: Whether one respondent can hold several of these values at once, which
    #: is a checkbox. It changes what selecting two of them means.
    multiple: bool = False
    #: The answers actually given, commonest first among the write-ins.
    #: Authored options are always listed, in their authored order, even at
    #: zero -- "nobody chose this" is worth being able to see before filtering
    #: by it.
    values: list[SurveyFacetValue] = []
    #: The lowest and highest answer, as text in the item's own spelling: a
    #: number, or an ISO date, datetime or time. What the range control opens
    #: on, so a slider does not have to guess its own ends.
    low: str | None = None
    high: str | None = None
    #: Interviews that answered this item at all. The denominator the counts
    #: above are read against.
    n_answered: int = 0


class SurveyFacets(_BaseModel):
    """Every survey item a project's interviews carry an answer to."""

    items: list[SurveyFacet] = []


class CodeFacet(_BaseModel):
    """One code, with how much of the current view carries it."""

    code_id: UUID4
    #: Chunks coded with this code exactly. What clicking *filter* on the row
    #: would return, so a badge is never an invitation into an empty view.
    count: int = 0
    #: Chunks coded with it or with anything under it -- what `/*` returns.
    #: Counted over the union of the branch and not summed down it: a chunk
    #: carrying both a parent and its child is one chunk, and adding the rows
    #: up would report it twice.
    subtree: int = 0


class CodeFacets(_BaseModel):
    """What each code in the codebook is worth over the chunks now in view."""

    #: The unit the counts are in. A code applied to one message is one
    #: MESSAGE, one QA_PAIR and one INTERVIEW, so the number only means
    #: something next to the unit it was counted in.
    kind: EmbeddingKind
    #: Chunks in view at all -- the denominator the counts are read against.
    total: int = 0
    #: Only the codes something in view carries; a code at zero is left out
    #: and the client reads a missing code as zero.
    items: list[CodeFacet] = []


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
