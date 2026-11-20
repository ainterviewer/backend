"""platform versions as YYYY.MM-NN

Revision ID: 5d2e8b7c1a94
Revises: 3f1c8a6d2e47
Create Date: 2026-11-20 12:00:00

"""

import re
from collections.abc import Callable, Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5d2e8b7c1a94"
down_revision: str | None = "3f1c8a6d2e47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The platform repo now names releases YYYY.MM-NN instead of YYYY.MM.N
# (2026.11.13 -> 2026.11-13): the zero-padded counter keeps its archive files
# in release order, and the hyphen keeps a version from reading as a date. It
# renamed every archived manifest, so the copies stored here are renamed to
# match, or `set-release-highlights` could not find a release by its file name.
# Nothing keeps translating the old spelling afterwards; these are all the
# copies:
#
# - `platform_release.platform_release_version`, and the `platform_version`
#   inside its stored manifest.
# - `interview.platform_version`, the release each interview ran on.
#
# A version in neither format (a hand-inserted dev row, say) is left alone, as
# is one already converted, so the upgrade can run over a database the new
# platform scripts have already inserted a release into.

OLD = re.compile(r"^(\d{4})\.(\d{2})\.(\d+)$")
NEW = re.compile(r"^(\d{4})\.(\d{2})-(\d+)$")


def to_new(version: str) -> str | None:
    match = OLD.match(version)
    return match and f"{match[1]}.{match[2]}-{int(match[3]):02d}"


def to_old(version: str) -> str | None:
    match = NEW.match(version)
    return match and f"{match[1]}.{match[2]}.{int(match[3])}"


def rewrite(convert: Callable[[str], str | None]) -> None:
    connection = op.get_bind()

    releases = connection.execute(
        sa.text(
            "SELECT id, platform_release_version, platform_manifest"
            " FROM platform_release"
        ).columns(
            sa.column("id"),
            sa.column("platform_release_version", sa.String),
            sa.column("platform_manifest", sa.JSON),
        )
    ).all()
    existing = {version for _, version, _ in releases}
    update_release = sa.text(
        "UPDATE platform_release"
        " SET platform_release_version = :version, platform_manifest = :manifest"
        " WHERE id = :id"
    ).bindparams(sa.bindparam("manifest", type_=sa.JSON))
    for release_id, version, manifest in releases:
        renamed = convert(version)
        if renamed is None:
            continue
        if renamed in existing:
            # The column is unique: both spellings of one release would mean
            # it was inserted twice, which needs a person to pick one.
            raise RuntimeError(f"platform_release has both {version!r} and {renamed!r}")
        if manifest.get("platform_version") == version:
            manifest = {**manifest, "platform_version": renamed}
        connection.execute(
            update_release,
            {"id": release_id, "version": renamed, "manifest": manifest},
        )

    versions = connection.execute(
        sa.text(
            "SELECT DISTINCT platform_version FROM interview"
            " WHERE platform_version IS NOT NULL"
        )
    ).scalars()
    update_interviews = sa.text(
        "UPDATE interview SET platform_version = :new WHERE platform_version = :old"
    )
    for version in list(versions):
        renamed = convert(version)
        if renamed is not None:
            connection.execute(update_interviews, {"old": version, "new": renamed})


def upgrade() -> None:
    rewrite(to_new)


def downgrade() -> None:
    rewrite(to_old)
