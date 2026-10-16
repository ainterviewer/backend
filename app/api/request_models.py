import datetime
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Query
from pydantic import (
    UUID4,
    AfterValidator,
    BaseModel,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

from ainterviewer.constants import LANGUAGES
from ainterviewer.interview_guides import InterviewGuide
from ainterviewer.synthesize.interviewees import BackgroundInfoOptions
from ainterviewer.types import (
    Feedback,
    Interviewer,
    InterviewStatus,
    LanguageCode,
    TestType,
)

from ..db.types import InterviewType
from ..types import ExternalParam, ProjectStatus

KNOWN_LANGUAGE_CODES = frozenset(entry["code"] for entry in LANGUAGES)


def _known_language(code: str) -> str:
    """Reject a well-formed code that names no language.

    `LanguageCode` only checks the shape, which lets a plausible wrong answer
    through: "DK" is Denmark's country code and not a language, so filtering on
    it matches nothing and looks like "this project has no Danish data" rather
    than like a typo.
    """
    if code not in KNOWN_LANGUAGE_CODES:
        raise ValueError(f"Unknown language code: {code}")
    return code


#: A language code as a *filter* value, validated against the known set so a
#: bad one is a 422 rather than an empty result set or a 500 from the column
#: type. Uppercased on the way in, since `LanguageCode` carries `to_upper`.
LanguageFilter = Annotated[LanguageCode, AfterValidator(_known_language)]


class PaginatedQueryParams(BaseModel):
    offset: int = Query(default=0, le=100)
    limit: int = Query(default=20, le=100)
    column: str = Query("created_at", description="Column to sort by")
    order: Literal["asc", "desc"] = Query("desc", description="Sorting order")


class InterviewFilters(BaseModel):
    """The interview list's filters, as the list endpoint takes them.

    Lets a bulk action target "everything matching" rather than ids the client
    has seen: with server-side paging it only ever holds one page of them.
    Same names and same defaults as the query parameters of `get_interviews`,
    so "all 123 selected" means exactly the 123 the list counted.
    """

    interview_types: list[InterviewType] | None = None
    types: list[InterviewType] | None = None
    statuses: list[InterviewStatus] | None = None
    languages: list[str] | None = None
    created_from: datetime.datetime | None = None
    created_to: datetime.datetime | None = None
    completed: bool | None = None
    search: str | None = Field(default=None, max_length=200)
    pid: str | None = Field(default=None, max_length=200)
    reported: bool | None = None
    intervened: bool | None = None


class InterviewSelectionRequest(BaseModel):
    """Interviews picked either by id or by the list's filters, never both."""

    interview_ids: list[UUID4] | None = None
    filters: InterviewFilters | None = None

    @model_validator(mode="after")
    def _exactly_one_selection(self):
        if (self.interview_ids is None) == (self.filters is None):
            raise ValueError("Give exactly one of interview_ids or filters.")
        return self


class DeleteInterviewRequest(InterviewSelectionRequest): ...


def _known_timezone(name: str | None) -> str | None:
    if name is None:
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"Unknown time zone: {name}")
    return name


class ExportMessagesRequest(InterviewSelectionRequest):
    """`csv` and `xlsx` export the message table, `docx` and `pdf` readable
    transcripts. `archive` makes it a ZIP with one file per interview."""

    format: Literal["csv", "xlsx", "docx", "pdf"] = "csv"
    archive: bool = False
    #: IANA name the transcript times are shown in, normally the browser's.
    #: The app's own time zone when absent.
    timezone: Annotated[str | None, AfterValidator(_known_timezone)] = None


class PromptRequest(BaseModel):
    prompt: str


class InterviewGuideGenerationRequest(PromptRequest): ...


class QuestionSectionGenerationRequest(PromptRequest): ...


class QuestionGenerationRequest(PromptRequest):
    section_id: UUID4
    max_probes_n: int | None = None
    max_probes_time: int | None = None


class ProjectStatusChangeRequest(BaseModel):
    status: ProjectStatus


class ProjectTitleUpdateRequest(BaseModel):
    title: str


class MoveProjectRequest(BaseModel):
    folder_id: UUID4


class CreateProjectRequest(BaseModel):
    title: str
    default_language: LanguageCode


class LoginData(BaseModel):
    email: str
    password: str
    extended: bool = False


class VerifyEmailRequest(BaseModel):
    token: str


class ResendVerificationRequest(BaseModel):
    email: EmailStr


class VerifyLoginCodeRequest(BaseModel):
    email: str
    code: str
    extended: bool = False


class UpdateEmailRequest(BaseModel):
    password: str
    new_email: EmailStr


class _NewPasswordRequest(BaseModel):
    new_password: str = Field(min_length=8)

    @field_validator("new_password")
    @classmethod
    def _password_within_bcrypt_limit(cls, value: str) -> str:
        # bcrypt silently truncates input beyond 72 bytes (not chars), so
        # reject anything longer to avoid surprising password equivalence.
        if len(value.encode("utf-8")) > 72:
            raise ValueError("Password must be at most 72 bytes")
        return value


class UpdatePasswordRequest(_NewPasswordRequest):
    current_password: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(_NewPasswordRequest):
    token: str


class DeleteAccountRequest(BaseModel):
    password: str


class CreateInterviewRequest(BaseModel):
    interviewer: Interviewer = Interviewer.AI
    interview_type: InterviewType = InterviewType.DISTRIBUTED
    test_run_id: UUID4 | None = None
    experiment_id: UUID4 | None = None
    synthetic_test_type: TestType | None = None
    participant_id: UUID4 | None = None
    pid: str | None = None

    # Extra context
    external_params: dict[str, Any] | None = Field(
        None,
        description="Extra query parameters, can include external user-id or the like.",
    )
    referer: str | None = Field(None, description="The referer of original request")


class ValidateExternalParamsRequest(BaseModel):
    external_params: dict[str, Any] | None = Field(
        None,
        description="Query parameters carried by the interview link.",
    )


class SynthesizeRequest(BaseModel):
    answering_model: str | None = None
    n_interviews: int = Field(ge=1, le=100)
    language: LanguageCode = "EN"
    delay_before_answers: tuple[float, float] | None = None


class UpdateBackgroundInfoRequest(BaseModel):
    background_info: BackgroundInfoOptions


class UpdateFixedPersonasRequest(BaseModel):
    fixed_personas: list[str]


class UpdateFixedAnswersRequest(BaseModel):
    #: By guide question id. A blank answer is left out: the question then
    #: has none, and the synthetic interviewee skips it.
    answers: dict[UUID4, str]

    @field_validator("answers")
    @classmethod
    def validate_answers(cls, value: dict[UUID4, str]) -> dict[UUID4, str]:
        stripped = {key: answer.strip() for key, answer in value.items()}
        return {key: answer for key, answer in stripped.items() if answer}


class TestSetupRequest(BaseModel):
    project_id: UUID4
    test_id: UUID4


class AssistanceChatRequest(BaseModel):
    prompt: str
    guide: InterviewGuide


class DeleteParticipantsRequest(BaseModel):
    participant_ids: list[UUID4]


class ExportParticipantsRequest(BaseModel):
    participant_ids: list[UUID4] | None = None


class ParticipantEmailTemplateRequest(BaseModel):
    subject: str | None = None
    template: str | None = None


class SendParticipantEmailRequest(BaseModel):
    # If None, send to every participating participant with an email address.
    participant_ids: list[UUID4] | None = None


class ExternalParamsRequest(BaseModel):
    params: list[ExternalParam]

    @field_validator("params")
    @classmethod
    def validate_params(cls, values: list[ExternalParam]):
        if len(values) != len({x.name for x in values}):
            raise ValueError("External parameter names must be unique")
        return values


class MessageFeedbackRequest(BaseModel):
    """Feedback on one message of the caller's own interview.

    Carries no interview or project id on purpose: both come from the
    interview_token cookie, so a caller cannot leave feedback on somebody
    else's transcript by naming it. Mirrors SpeechRequest.
    """

    # Interview-scoped, so it only identifies a message together with the
    # interview the token names.
    message_id: int
    feedback: Feedback | None = None


class SpeechRequest(BaseModel):
    # The interview-scoped integer id of the message to synthesize. The text is
    # looked up server-side so participants can't synthesize arbitrary text.
    message_id: int
