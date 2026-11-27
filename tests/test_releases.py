"""`GET /releases` serves a preview feature's highlights only to its users.

Everyone else still gets the release, curated-but-empty when it announced
nothing but preview features, so it reads as a maintenance release.
"""

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.main import releases
from app.auth import AuthToken
from app.db.crud import InterviewDataBase
from app.db.tables import Base
from app.platform_release import (
    LEGACY_PREVIEW,
    GitHashes,
    Highlight,
    PlatformManifest,
)
from app.types import PreviewFeature, Scope


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
# Curated before highlights named their feature.
PREVIEW = Highlight.model_validate(
    {"kind": "new", "title": "Explore your interviews", "audience": "preview"}
)
CODEBOOK = Highlight(kind="new", title="Code your interviews", feature="codebook")


@pytest.fixture
def db():
    """Releases stored for real, so `feature` must survive the JSONB round trip."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        db = InterviewDataBase(session)
        db.set_platform_release(manifest("2026.10-01", None))
        db.set_platform_release(manifest("2026.10-02", [GENERAL, PREVIEW]))
        db.set_platform_release(manifest("2026.10-03", [PREVIEW]))
        db.set_platform_release(manifest("2026.10-04", [CODEBOOK]))
        yield db


def token(*features: PreviewFeature) -> AuthToken:
    return AuthToken(
        user_id=uuid.uuid4(), scope=Scope.USER, preview_features=list(features)
    )


def served(db: InterviewDataBase, token: AuthToken | None) -> dict[str, list[str]]:
    return {
        release.platform_version: [h.title for h in release.highlights]
        for release in releases(db=db, token=token)
    }


def test_a_features_users_see_its_highlights(db):
    assert served(db, token(PreviewFeature.CODEBOOK)) == {
        "2026.10-04": [CODEBOOK.title],
        "2026.10-03": [PREVIEW.title],
        "2026.10-02": [GENERAL.title, PREVIEW.title],
    }


def test_a_highlight_naming_no_feature_reaches_any_preview_user(db):
    """Curated before features were told apart, or about one since graduated."""
    graduated = PREVIEW.model_copy(update={"feature": "explore"})
    db.set_platform_release(manifest("2026.10-05", [graduated]))
    assert served(db, token(PreviewFeature.REPORT)) == {
        "2026.10-05": [graduated.title],
        "2026.10-04": [],
        "2026.10-03": [PREVIEW.title],
        "2026.10-02": [GENERAL.title, PREVIEW.title],
    }


def test_other_users_see_preview_only_releases_as_empty(db):
    expected = {"2026.10-04": [], "2026.10-03": [], "2026.10-02": [GENERAL.title]}
    assert served(db, token()) == expected
    assert served(db, None) == expected


def test_highlights_curated_before_audience_existed_reach_everyone():
    assert Highlight.model_validate({"kind": "new", "title": "x"}).feature is None


@pytest.mark.parametrize(
    ("stored", "feature"),
    [
        ({"audience": "all"}, None),
        ({"audience": "preview"}, LEGACY_PREVIEW),
        ({"audience": "preview", "feature": "report"}, "report"),
    ],
)
def test_audience_reads_as_a_feature(stored, feature):
    highlight = Highlight.model_validate({"kind": "new", "title": "x"} | stored)
    assert highlight.feature == feature
    assert "audience" not in highlight.model_dump()


@pytest.mark.parametrize("version", ["2026.11.13", "2026.11-1", "latest"])
def test_a_manifest_needs_a_yyyy_mm_nn_version(version):
    """An insert from platform scripts that predate YYYY.MM-NN fails the deploy
    step, rather than storing a release the rename migration never saw."""
    with pytest.raises(ValidationError, match="platform_version"):
        manifest(version, None)
