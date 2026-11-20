"""Sample release history for the development What's new feed."""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.db.tables import PlatformReleaseTable
from app.platform_release import GitHashes, Highlight, PlatformManifest


def create(session: Session) -> None:
    # These are fictional development releases. Include every curation state
    # and audience so both the public and preview feeds have useful fixtures.
    releases: list[tuple[str, datetime, list[Highlight] | None]] = [
        ("2026.10.1", datetime(2026, 10, 1, 12, tzinfo=UTC), None),
        (
            "2026.10.2",
            datetime(2026, 10, 5, 12, tzinfo=UTC),
            [
                Highlight(
                    kind="new",
                    title="Run interviews in multiple languages",
                    body="Publish translated guides and let participants choose their language.",
                ),
                Highlight(
                    kind="fixed",
                    title="Resume interrupted interviews",
                    body="Participants can reconnect and continue where they left off.",
                ),
            ],
        ),
        ("2026.10.3", datetime(2026, 10, 9, 12, tzinfo=UTC), []),
        (
            "2026.10.4",
            datetime(2026, 10, 13, 12, tzinfo=UTC),
            [
                Highlight(
                    kind="new",
                    title="Explore related interview answers",
                    body="Find similar answers across your project with semantic search.",
                    audience="preview",
                ),
            ],
        ),
        (
            "2026.10.5",
            datetime(2026, 10, 17, 12, tzinfo=UTC),
            [
                Highlight(
                    kind="improved",
                    title="Organise projects with your team",
                    body="Group studies into folders and invite collaborators to help.",
                ),
                Highlight(
                    kind="improved",
                    title="Explore answers by language",
                    body="Filter the exploration view to focus on selected interview languages.",
                    audience="preview",
                ),
                Highlight(kind="fixed", title="Project update times stay consistent"),
            ],
        ),
    ]
    for version, released_at, highlights in releases:
        session.add(
            PlatformReleaseTable(
                platform_release_version=version,
                created_at=released_at,
                platform_manifest=PlatformManifest(
                    platform_version=version,
                    core_lib="v0.1.0-dev",
                    backend="v0.1.0-dev",
                    frontend="v0.1.0-dev",
                    build_time=released_at,
                    git=GitHashes(
                        core_lib="0" * 40, backend="0" * 40, frontend="0" * 40
                    ),
                    highlights=highlights,
                ),
            )
        )
    session.commit()
