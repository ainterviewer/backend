"""Test-suite settings bootstrap.

``app.settings`` instantiates ``AppSecrets`` at import time, so importing any
module under ``app`` requires the secrets to be present. Likewise the
assistance agent builds an OpenAI provider at import, so anything importing
the full router (``app.api.main``) needs an API key, though no test calls it. Developers get them
from a gitignored ``.env``; CI and fresh clones have none, which turns every
test module into a collection error. Seed throwaway values here -- pytest
imports this file before any test module, so it lands before the first import
of ``app``. ``setdefault`` keeps a real environment authoritative.
"""

import os

os.environ.setdefault("APP_SECRET__JWT_SECRET_KEY", "test-jwt-secret")
os.environ.setdefault("APP_SECRET__SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
