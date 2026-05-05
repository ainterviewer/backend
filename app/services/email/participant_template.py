"""Scaffolding for rendering participant invitation email templates.

Templates are stored per-localization on `ProjectLocalizationTable.participant_email_template`
and authored by users in Jinja2 syntax. This module centralizes the template
environment and the variables exposed to the template so additional validation
hooks can be added later (e.g. checking for required placeholders, max length,
disallowed tags).
"""

from typing import Any

from jinja2 import Environment, StrictUndefined, TemplateSyntaxError, select_autoescape
from jinja2.sandbox import SandboxedEnvironment

# Variables that are guaranteed to be available inside a participant email
# template. Extend this list (and `build_template_context`) when new fields
# are exposed to template authors.
ALLOWED_TEMPLATE_VARIABLES: tuple[str, ...] = (
    "participant_name",
    "participant_email",
    "participant_pid",
    "interview_url",
    "project_title",
)


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
    participant_name: str | None,
    participant_email: str | None,
    participant_pid: str | None,
    interview_url: str | None = None,
    project_title: str | None = None,
) -> dict[str, Any]:
    return {
        "participant_name": participant_name or "",
        "participant_email": participant_email or "",
        "participant_pid": participant_pid or "",
        "interview_url": interview_url or "",
        "project_title": project_title or "",
    }


def render_participant_email_template(template: str, context: dict[str, Any]) -> str:
    return participant_template_env.from_string(template).render(**context)


__all__ = [
    "ALLOWED_TEMPLATE_VARIABLES",
    "build_template_context",
    "participant_template_env",
    "render_participant_email_template",
    "validate_participant_email_template",
    "TemplateSyntaxError",
]
