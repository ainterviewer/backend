"""The assistant's model comes from settings and is built only when used.

Built at import, its provider client refused an empty API key and took the
whole backend down on any deployment without an OpenAI key.
"""

from types import SimpleNamespace

import pytest
from pydantic import SecretStr, ValidationError
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.openrouter import OpenRouterModel

from app.services.assistance import agent
from app.settings import AssistanceSettings


@pytest.fixture
def build(monkeypatch):
    """Build the model for `model` with placeholder keys, outside the cache."""
    secrets = SimpleNamespace(
        openai_api_key=SecretStr("openai-key"),
        openrouter_api_key=SecretStr("openrouter-key"),
    )
    monkeypatch.setattr(agent, "lib_settings", SimpleNamespace(secrets=secrets))

    def _build(model: str):
        monkeypatch.setattr(agent, "ASSISTANCE_MODEL", model)
        return agent.assistance_model.__wrapped__()

    return _build


def test_agent_has_no_model_until_a_run():
    assert agent.assistance_agent.model is None


def test_openai_model(build):
    model = build("openai:gpt-6.1-terra")

    assert type(model) is OpenAIChatModel
    assert model.model_name == "gpt-6.1-terra"


def test_openrouter_model(build):
    model = build("openrouter:openai/gpt-oss-120b")

    assert isinstance(model, OpenRouterModel)
    assert model.model_name == "openai/gpt-oss-120b"


def test_unsupported_provider_is_rejected():
    with pytest.raises(ValidationError, match="must start with one of"):
        AssistanceSettings(model="gemini:gemini-3-flash")
