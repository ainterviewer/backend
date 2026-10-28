"""Deployment-specific addresses come from settings, never from the code.

A self-hosted instance must not send its participants to the hosted platform:
not in links, not in the contact address, not in email footers.
"""

import pytest
from pydantic import ValidationError

from app.api.misc import get_instance
from app.services.email.mail import email_templates
from app.settings import Settings, app_settings


@pytest.fixture
def bare_env(monkeypatch, tmp_path):
    """A working directory with no config.toml or .env, as on a fresh deploy."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_SECRET__JWT_SECRET_KEY", "x")
    monkeypatch.setenv("APP_SECRET__SESSION_SECRET_KEY", "y")
    return monkeypatch


def test_development_runs_on_defaults(bare_env):
    bare_env.setenv("APP_ENV", "development")

    assert Settings().app.contact_email == "contact@example.org"


@pytest.mark.parametrize("app_env", ["production", "staging"])
def test_deployed_environment_requires_its_own_addresses(bare_env, app_env):
    bare_env.setenv("APP_ENV", app_env)

    with pytest.raises(ValidationError, match="public_url and app.contact_email"):
        Settings()


def test_deployed_environment_starts_once_addresses_are_set(bare_env):
    bare_env.setenv("APP_ENV", "production")
    bare_env.setenv("APP__PUBLIC_URL", "https://interviews.example.org/")
    bare_env.setenv("APP__CONTACT_EMAIL", "privacy@example.org")

    settings = Settings()

    assert settings.app.public_url == "https://interviews.example.org"
    assert settings.app.contact_email == "privacy@example.org"


def test_instance_endpoint_serves_the_configured_addresses():
    instance = get_instance()

    assert instance.contact_email == app_settings.app.contact_email
    assert instance.website_url == app_settings.app.website_url
    assert instance.docs_url == app_settings.app.docs_url


def test_email_footer_uses_the_configured_addresses():
    html = email_templates.from_string('{% extends "base_email.jinja" %}').render(
        recipient_name="Ada"
    )

    assert f"mailto:{app_settings.app.contact_email}" in html
    assert f'href="{app_settings.app.website_url}"' in html
    assert f"{app_settings.app.public_url}/images/email_header.png" in html
