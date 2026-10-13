"""`GET /releases` serves preview-feature highlights only to preview users.

Everyone else still gets the release, curated-but-empty when it announced
nothing but preview features, so it reads as a maintenance release.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.main import releases
from app.auth import AuthToken
from app.db.crud import InterviewDataBase
from app.db.tables import Base
from app.platform_release import GitHashes, Highlight, PlatformManifest
from app.types import Scope


def manifest(version: str, highlights: list[Highlight] | None) -> PlatformManifest:
    return PlatformManifest(
        platform_version=version,
        core_lib="v0.1.0",
        backend="v0.1.0",
        frontend="v0.1.0",
        build_time=datetime(2026, 10, 13, tzinfo=UTC),
        git=GitHashes(core_lib="a", backend="b", frontend="c"),
        highlights=highlights,
    )


GENERAL = Highlight(kind="fixed", title="Interviews run in their own language")
PREVIEW = Highlight(kind="new", title="Explore your interviews", audience="preview")


@pytest.fixture
def db():
    """Releases stored for real, so `audience` must survive the JSONB round trip."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        db = InterviewDataBase(session)
        db.set_platform_release(manifest("2026.10.1", None))
        db.set_platform_release(manifest("2026.10.2", [GENERAL, PREVIEW]))
        db.set_platform_release(manifest("2026.10.3", [PREVIEW]))
        yield db


def token(preview_features: bool) -> AuthToken:
    return AuthToken(
        user_id=uuid.uuid4(), scope=Scope.USER, preview_features=preview_features
    )


def served(db: InterviewDataBase, token: AuthToken | None) -> dict[str, list[str]]:
    return {
        release.platform_version: [h.title for h in release.highlights]
        for release in releases(db=db, token=token)
    }


def test_preview_users_see_every_highlight(db):
    assert served(db, token(preview_features=True)) == {
        "2026.10.3": [PREVIEW.title],
        "2026.10.2": [GENERAL.title, PREVIEW.title],
    }


def test_other_users_see_preview_only_releases_as_empty(db):
    expected = {"2026.10.3": [], "2026.10.2": [GENERAL.title]}
    assert served(db, token(preview_features=False)) == expected
    assert served(db, None) == expected


def test_highlights_curated_before_audience_existed_default_to_all():
    assert Highlight.model_validate({"kind": "new", "title": "x"}).audience == "all"
