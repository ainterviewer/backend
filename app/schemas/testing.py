"""Test setups, test runs and synthetic interviews."""

from __future__ import annotations

from datetime import datetime

from pydantic import UUID4, Field, field_validator

from ainterviewer.settings import settings as lib_settings
from ainterviewer.synthesize.interviewees import BackgroundInfoOptions
from ainterviewer.types import LanguageCode, TestType

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
    answering_model: str
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
