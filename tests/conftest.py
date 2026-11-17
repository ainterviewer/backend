"""Test-suite settings bootstrap.

``app.settings`` instantiates ``AppSecrets`` at import time, so importing any
module under ``app`` requires the secrets to be present. Likewise the
assistance agent builds its provider (OpenRouter by default) at import, so
anything importing the full router (``app.api.main``) needs that provider's API
key, though no test calls it. Developers get them
from a gitignored ``.env``; CI and fresh clones have none, which turns every
test module into a collection error. Seed throwaway values here -- pytest
imports this file before any test module, so it lands before the first import
of ``app``. ``setdefault`` keeps a real environment authoritative.

The database is the exception. ``app.dependencies`` builds the app's engine at
import from ``DATABASE__DB_PATH``, which defaults to the gitignored
``storage/``: a test that reaches it (a ``with TestClient(app)`` running the
lifespan, say) wrote to the developer's database locally and failed in CI,
which has no ``storage/``. Point it at a fresh directory instead, and assign it
rather than ``setdefault`` it -- no test should ever reach a real database.

The model lists are assigned too: which models a project may publish with is
read from the gitignored ``config.toml``, whose absence (CI) would make every
model test only and refuse every publish. Pinned here, the default model may be
distributed and ``TEST_ONLY_MODEL`` may not. Speech is not pinned: an
``APP_SERVICE__`` variable loses to a ``[services]`` table in ``config.toml``,
so a test that needs it patches ``app.distribution.speech_is_test_only``.
"""

import json
import os
import tempfile

TEST_ONLY_MODEL = "openrouter:a-test-only-model"

os.environ.setdefault("APP_SECRET__JWT_SECRET_KEY", "test-jwt-secret")
os.environ.setdefault("APP_SECRET__SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("OPENROUTER_API_KEY", "test-openrouter-key")
os.environ["DATABASE__DB_PATH"] = tempfile.mkdtemp(prefix="ainterviewer-test-")
os.environ["LLM__DEFAULT_MODEL"] = "openrouter:openai/gpt-oss-120b"
os.environ["LLM__MODELS"] = json.dumps(
    ["openrouter:openai/gpt-oss-120b", "openai/gpt-oss-120b"]
)
os.environ["LLM__TEST_ONLY_MODELS"] = json.dumps([TEST_ONLY_MODEL])
os.environ["LLM__DEMO_MODELS"] = json.dumps(["openrouter:openai/gpt-oss-120b"])
os.environ["LLM__DEFAULT_DEMO_MODEL"] = "openrouter:openai/gpt-oss-120b"
