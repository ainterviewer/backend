"""release highlights name their preview feature instead of an audience

A highlight was `audience: "all" | "preview"`; it now names the
`PreviewFeature` it is about, or none for everyone. The stored preview
highlights get the feature they describe, matched by title. One this does
not know gets "preview", which no feature is called, so it still reaches
only users with some preview feature (`LEGACY_PREVIEW`).

Revision ID: e5b8d03a7f12
Revises: c7e2a9d41b6f
Create Date: 2026-11-27 14:00:00.000000

"""

# NOTE: Don't manage the touch-last_updated triggers here. alembic/env.py
# uninstalls them before every run and reinstalls the full set from
# app/db/triggers.py after, so batch_alter_table on `project`, `testrun` etc.
# cannot lose or break them.
import json
from collections.abc import Callable, Sequence

import sqlalchemy as sa
from alembic import op

import app.db.types  # noqa: F401

# revision identifiers, used by Alembic.
revision: str = "e5b8d03a7f12"
down_revision: str | None = "c7e2a9d41b6f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Written out rather than imported, so this keeps meaning the same.
_LEGACY_PREVIEW = "preview"
# The preview highlights curated before features were told apart.
_FEATURE_BY_TITLE = {
    "Explore and search across your interviews": "codebook",
    "Code your transcripts with the new codebook": "codebook",
    "New report view for your analysis results": "report",
    "Flag and review unsafe interview messages": "safety",
    "Download attachments from participant emails": "participants",
}


def _rewrite(change: Callable[[dict], None]) -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text("SELECT id, platform_manifest FROM platform_release")
    ).all()
    for row_id, raw in rows:
        manifest = json.loads(raw) if isinstance(raw, str) else raw
        highlights = manifest.get("highlights")
        if not highlights:
            continue
        for highlight in highlights:
            change(highlight)
        connection.execute(
            sa.text(
                "UPDATE platform_release SET platform_manifest = :manifest "
                "WHERE id = :id"
            ),
            {"manifest": json.dumps(manifest), "id": row_id},
        )


def _to_feature(highlight: dict) -> None:
    audience = highlight.pop("audience", "all")
    if audience == "preview" and not highlight.get("feature"):
        highlight["feature"] = _FEATURE_BY_TITLE.get(
            highlight.get("title", ""), _LEGACY_PREVIEW
        )
    highlight.setdefault("feature", None)


def _to_audience(highlight: dict) -> None:
    feature = highlight.pop("feature", None)
    highlight["audience"] = "all" if feature is None else "preview"


def upgrade() -> None:
    _rewrite(_to_feature)


def downgrade() -> None:
    _rewrite(_to_audience)
