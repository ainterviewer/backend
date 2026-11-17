"""Test setups, test runs and synthetic interviews."""

from __future__ import annotations

from datetime import datetime

from pydantic import UUID4, Field, field_validator

from ainterviewer.settings import settings as lib_settings
from ainterviewer.synthesize.interviewees import BackgroundInfoOptions, InterviewSubject
from ainterviewer.types import InterviewStatus, LanguageCode, MessageRole, TestType

from ..types import TestRunStatus
from .common import Schema


class TestSetupBase(Schema):
    name: str | None = None
    type: TestType
    project_id: UUID4
    # A factory, so the default is read when a setup is made rather than once
    # at import.
    answering_model: str = Field(default_factory=lambda: lib_settings.llm.default_model)
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
    fixed_answers: dict[UUID4, str] | None = None
    fixed_personas: list[str] | None = None


class TestRunBase(Schema):
    test_setup_id: UUID4
    language: LanguageCode = "EN"
    n_interviews: int
    #: Null for a FIXED_ANSWERS run, which has no answering agent.
    answering_model: str | None = None
    #: The model every interview agent ran on in place of the project's
    #: agent configs, to compare models; null for the project's own.
    interviewer_model: str | None = None
    delay_before_answers: tuple[float, float] | None = None


class TestRunCreate(TestRunBase):
    started_by_id: UUID4 | None = None


class TestRunPublic(TestRunBase):
    id: UUID4
    created_at: datetime
    last_updated: datetime | None = None
    status: TestRunStatus


class SynthesizeRequest(Schema):
    answering_model: str | None = None
    n_interviews: int = Field(ge=1, le=100)
    language: LanguageCode = "EN"
    delay_before_answers: tuple[float, float] | None = None
    #: Run the interviews on each of these models in place of the project's
    #: agent configs: one run per model, the same synthetic interviewees in
    #: each, so the runs can be compared. Empty for one run on the project's
    #: own models. Any model may be tried, test-only ones included.
    interviewer_models: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("interviewer_models")
    @classmethod
    def unique_models(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


class UpdateBackgroundInfoRequest(Schema):
    background_info: BackgroundInfoOptions


class UpdateFixedPersonasRequest(Schema):
    fixed_personas: list[str]


class UpdateFixedAnswersRequest(Schema):
    #: By guide question id. A blank answer is left out: the question then
    #: has none, and the synthetic interviewee skips it.
    answers: dict[UUID4, str]

    @field_validator("answers")
    @classmethod
    def validate_answers(cls, value: dict[UUID4, str]) -> dict[UUID4, str]:
        stripped = {key: answer.strip() for key, answer in value.items()}
        return {key: answer for key, answer in stripped.items() if answer}


class SynthesizeResponse(Schema):
    project_id: UUID4
    message: str
    status: str
    #: One per interviewer model asked for, in that order; one otherwise.
    test_run_ids: list[UUID4] = []


class ComparisonTurn(Schema):
    """One message of a compared interview, as much of it as a side-by-side
    reading needs."""

    role: MessageRole
    content: str
    main_question: int | None = None
    sub_question: int | None = None


class ComparisonInterview(Schema):
    interview_id: UUID4
    status: InterviewStatus
    #: Who the synthetic interviewee played, as stored: what interviews are
    #: paired on across runs. Null if it was never recorded.
    persona: InterviewSubject | str | None
    turns: list[ComparisonTurn]


class RunComparison(Schema):
    run: TestRunPublic
    #: Oldest first, which is the order the interviewees were started in.
    interviews: list[ComparisonInterview]


class RunComparisonResponse(Schema):
    runs: list[RunComparison]
