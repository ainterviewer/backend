"""Test-suite settings bootstrap.

``app.settings`` instantiates ``AppSecrets`` at import time, so importing any
module under ``app`` requires the secrets to be present. Likewise the
assistance agent builds an OpenAI provider at import, so anything importing
the full router (``app.api.main``) needs an API key, though no test calls it. Developers get them
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
"""

import os
import tempfile

os.environ.setdefault("APP_SECRET__JWT_SECRET_KEY", "test-jwt-secret")
os.environ.setdefault("APP_SECRET__SESSION_SECRET_KEY", "test-session-secret")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ["DATABASE__DB_PATH"] = tempfile.mkdtemp(prefix="ainterviewer-test-")
