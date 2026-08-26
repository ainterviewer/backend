"""Scaffolding for rendering participant invitation email templates.

Templates are stored per-localization on `ProjectLocalizationTable.participant_email_template`
and authored by users in Jinja2 syntax. This module centralizes the template
environment and the context builder, so additional validation hooks can be
added later (e.g. checking for required placeholders, max length, disallowed
tags).

`TemplatePlaceholder` is the authoritative list of what a template may
reference. It is exported into the OpenAPI schema (see `app/openapi.py`), so
the dashboard's template editor derives its insert buttons from this enum
instead of restating the names -- the two lists had already drifted apart
once, leaving `project_title` renderable on send but unsubstituted in the
editor's preview.
"""

from enum import StrEnum
from typing import Any

from jinja2 import Environment, StrictUndefined, TemplateSyntaxError, select_autoescape
from jinja2.sandbox import SandboxedEnvironment


class TemplatePlaceholder(StrEnum):
    """Every variable a participant email template may reference.

    Adding a member here is the only step needed on the backend; regenerating
    the frontend SDK then turns a missing label in the editor into a build
    error rather than a silently unavailable placeholder.
    """

    NAME = "name"
    EMAIL = "email"
    PID = "pid"
    INTERVIEW_URL = "interview_url"
    PROJECT_TITLE = "project_title"
    OPT_OUT_URL = "opt_out_url"


def _build_environment() -> Environment:
    return SandboxedEnvironment(
        autoescape=select_autoescape(default_for_string=True),
        undefined=StrictUndefined,
    )


participant_template_env: Environment = _build_environment()


def validate_participant_email_template(template: str) -> None:
    """Parse the template and raise `TemplateSyntaxError` if invalid.

    Intended as a scaffolding hook — extra validation (required placeholders,
    length limits, disallowed constructs) can be layered on top later.
    """
    participant_template_env.parse(template)


def build_template_context(
    *,
    name: str | None,
    email: str | None,
    pid: str | None,
    interview_url: str | None = None,
    project_title: str | None = None,
    opt_out_url: str | None = None,
) -> dict[str, Any]:
    # Keyed off the enum so a renamed placeholder cannot leave a stale key here.
    return {
        TemplatePlaceholder.NAME.value: name or "",
        TemplatePlaceholder.EMAIL.value: email or "",
        TemplatePlaceholder.PID.value: pid or "",
        TemplatePlaceholder.INTERVIEW_URL.value: interview_url or "",
        TemplatePlaceholder.PROJECT_TITLE.value: project_title or "",
        TemplatePlaceholder.OPT_OUT_URL.value: opt_out_url or "",
    }


def render_participant_email_template(template: str, context: dict[str, Any]) -> str:
    return participant_template_env.from_string(template).render(**context)


__all__ = [
    "TemplatePlaceholder",
    "TemplateSyntaxError",
    "build_template_context",
    "participant_template_env",
    "render_participant_email_template",
    "validate_participant_email_template",
]
