"""Deployment-specific addresses come from settings, never from the code.

A self-hosted instance must not send its participants to the hosted platform:
not in links, not in the contact address, not in email footers.
"""

import pytest
from pydantic import ValidationError

from ainterviewer.settings import ConfigError, ConfigWarning
from app import settings as settings_module
from app.api.misc import get_instance
from app.services.email.mail import email_templates
from app.settings import AppSecrets, Settings, app_settings


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


# Secrets come from the environment only, so config.toml can be committed.


def _email_env(env):
    env.setenv("APP_SERVICE__EMAIL__SMTP_SERVER", "smtp.invalid")
    env.setenv("APP_SERVICE__EMAIL__SENDER__EMAIL", "info@example.org")
    env.setenv("APP_SERVICE__EMAIL__RECIPIENT__EMAIL", "admin@example.org")


def test_a_backend_secret_in_config_toml_names_its_variable(bare_env, tmp_path):
    (tmp_path / "config.toml").write_text('[app]\njwt_secret_key = "x"')

    with pytest.raises(ConfigError, match="APP_SECRET__JWT_SECRET_KEY"):
        Settings()


def test_email_needs_the_smtp_password(bare_env):
    _email_env(bare_env)
    bare_env.setattr(settings_module, "app_secrets", AppSecrets())

    with pytest.raises(ValidationError, match="APP_SECRET__SMTP_PASSWORD"):
        Settings()

    bare_env.setattr(settings_module, "app_secrets", AppSecrets(smtp_password="secret"))
    email = Settings().services.email
    assert email is not None
    assert email.sender.email == "info@example.org"


def test_the_old_smtp_password_variable_says_where_it_went(bare_env):
    _email_env(bare_env)
    bare_env.setenv("APP_SERVICE__EMAIL__SENDER__PASSWORD", "secret")

    with pytest.raises(ValidationError, match="APP_SECRET__SMTP_PASSWORD"):
        Settings()


def test_an_unknown_key_in_a_table_is_refused(bare_env, tmp_path):
    (tmp_path / "config.toml").write_text('[app]\npublic_ulr = "https://x"')

    with pytest.raises(ValidationError, match="public_ulr"):
        Settings()


def test_an_unknown_table_is_warned_about_but_the_librarys_are_not(bare_env, tmp_path):
    (tmp_path / "config.toml").write_text("[llm]\n[storage]\n[servics]")

    with pytest.warns(ConfigWarning, match="nothing reads servics;"):
        Settings()


def test_migrated_registration_tokens_only_warn(bare_env, tmp_path):
    (tmp_path / "config.toml").write_text(
        '[app]\nspecial_registration_tokens = [{token = "workshop", scope = "user"}]'
    )

    with pytest.warns(ConfigWarning, match="special_registration_tokens"):
        Settings()


def test_unrelated_dotenv_lines_are_not_services(bare_env, tmp_path):
    # pydantic-settings hands a model with extra="forbid" every .env line that
    # is not one of its fields, which turned every secret into an error.
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=sk-x\nGITHUB_TOKEN=ghp-x\nAPP_SECRET__SMTP_PASSWORD=x\n"
        "APP_SERVICE__SPEECH__TTS_VOICE=cedar\n"
    )

    assert Settings().services.speech.tts_voice == "cedar"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("APP__PUBLIC_ULR", "leaked-app-value"),
        ("APP_SERVICE__EMAIL__SENDER__PASSWORD", "leaked-smtp-password"),
    ],
)
def test_settings_errors_do_not_print_values(bare_env, tmp_path, name, value):
    # The environment carries the secrets, so an error must not echo it.
    _email_env(bare_env)
    (tmp_path / ".env").write_text(f"{name}={value}\nGHCR_TOKEN=leaked-token\n")

    with pytest.raises(ValidationError) as error:
        Settings()

    assert value not in str(error.value)
    assert "leaked-token" not in str(error.value)
