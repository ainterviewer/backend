"""Versions of a project's content: publish, snapshot, restore, prune.

The draft is `projectcontent`; a version is an immutable copy of its content
columns (`ContentColumns`) in `projectcontentversion`. Respondents read the
version `projectcontent.published_version_id` points at, test interviews a
snapshot of the draft, and the editor the draft itself.
"""

import copy
import hashlib
import json
from typing import Any

from pydantic import UUID4, BaseModel
from pydantic_core import to_jsonable_python
from sqlalchemy import delete, exists, func, select

from ainterviewer.utils import get_language_dict

from ..models import ContentStatus, ContentVersionPublic, ContentVersionSummary
from ..tables import (
    ContentColumns,
    InterviewTable,
    ProjectContentTable,
    ProjectContentVersionTable,
)
from ..types import ContentVersionKind
from ..versioning import FIRST_VERSION, Bump, SemVer, bumped, classify, format_semver
from .base import BaseRepository
from .errors import ContentRevisionError, NotPublishedError, PublishError

#: Automatic versions kept per project by `prune`, newest first. Ones an
#: interview runs on, and the published one, are kept beyond it.
KEEP_AUTOMATIC_VERSIONS = 20


def content_hash(source: ContentColumns) -> str:
    """sha256 of the content columns' canonical JSON, as the models dump it.

    Compares content, not bytes stored: two rows holding the same content hash
    the same whichever wrote them. It does change when the library adds a
    field to a model, so a stored hash only dedupes; `has_unpublished_changes`
    recomputes both sides.
    """
    payload = {
        field: to_jsonable_python(getattr(source, field))
        for field in ContentColumns.CONTENT_FIELDS
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _copied(value: Any) -> Any:
    return (
        value.model_copy(deep=True)
        if isinstance(value, BaseModel)
        else copy.deepcopy(value)
    )


def copy_content(source: ContentColumns, target: ContentColumns) -> None:
    """Write `source`'s content columns onto `target`, as copies."""
    for field in ContentColumns.CONTENT_FIELDS:
        setattr(target, field, _copied(getattr(source, field)))


def missing_text(source: ContentColumns) -> dict[str, list[str]]:
    """Per offered language, the paths of the text it lacks: written in some
    language, but not in it. Languages lacking nothing are left out."""
    parts = {
        "guide": source.guide,
        "consent": source.consent,
        "welcome": source.welcome,
        "emails": source.emails,
    }
    missing: dict[str, list[str]] = {}
    for language in source.languages:
        paths = [
            f"{name}.{path}"
            for name, part in parts.items()
            if part is not None
            for path, text in part.texts()
            if not text.get(language) and any(text.values())
        ]
        if paths:
            missing[language] = paths
    return missing


def _semver_of(version: ProjectContentVersionTable) -> SemVer:
    assert version.major is not None
    return (version.major, version.minor or 0, version.patch or 0)


class VersionRepository(BaseRepository):
    """Versions of a project's content. See the module docstring."""

    # ==================== Reading ====================

    def _content(self, project_id: UUID4) -> ProjectContentTable:
        return self.session.execute(
            select(ProjectContentTable).where(
                ProjectContentTable.project_id == project_id
            )
        ).scalar_one()

    def _version(
        self, project_id: UUID4, version_id: UUID4
    ) -> ProjectContentVersionTable:
        """The version, if it is this project's; NoResultFound otherwise."""
        return self.session.execute(
            select(ProjectContentVersionTable).where(
                ProjectContentVersionTable.id == version_id,
                ProjectContentVersionTable.project_id == project_id,
            )
        ).scalar_one()

    def published(self, project_id: UUID4) -> ProjectContentVersionTable:
        """What respondents get. Raises NotPublishedError if nothing is."""
        version = self._content(project_id).published_version
        if version is None:
            raise NotPublishedError("This study has not been published yet.")
        return version

    def check_revision(self, project_id: UUID4, revision: int) -> None:
        """Raise ContentRevisionError unless the draft is at `revision`."""
        current = self._content(project_id).revision
        if current != revision:
            raise ContentRevisionError(revision, current)

    def _summary(
        self, version: ProjectContentVersionTable, published_id: UUID4 | None
    ) -> ContentVersionSummary:
        author = version.created_by
        return ContentVersionSummary(
            id=version.id,
            version_no=version.version_no,
            kind=version.kind,
            semver=None
            if version.major is None
            else format_semver(_semver_of(version)),
            label=version.label,
            created_at=version.created_at,
            created_by_id=version.created_by_id,
            created_by_name=None
            if author is None
            else " ".join(filter(None, (author.first_name, author.last_name))),
            is_published=version.id == published_id,
            languages=version.languages,
            default_language=version.default_language,
        )

    def list_versions(
        self, project_id: UUID4, limit: int, offset: int
    ) -> tuple[list[ContentVersionSummary], int]:
        """The project's versions, newest first, and how many there are."""
        published_id = self._content(project_id).published_version_id
        versions = self.session.execute(
            select(ProjectContentVersionTable)
            .where(ProjectContentVersionTable.project_id == project_id)
            .order_by(ProjectContentVersionTable.version_no.desc())
            .limit(limit)
            .offset(offset)
        ).scalars()
        total = self.session.execute(
            select(func.count(ProjectContentVersionTable.id)).where(
                ProjectContentVersionTable.project_id == project_id
            )
        ).scalar_one()
        return [self._summary(v, published_id) for v in versions], total

    def get_version(self, project_id: UUID4, version_id: UUID4) -> ContentVersionPublic:
        version = self._version(project_id, version_id)
        summary = self._summary(version, self._content(project_id).published_version_id)
        return ContentVersionPublic(
            **summary.model_dump(),
            guide=version.guide,
            consent=version.consent,
            welcome=version.welcome,
            emails=version.emails,
            agent_configs=version.agent_configs,
            prompt_overrides=dict(version.prompt_overrides),
        )

    def _latest_semver(self, project_id: UUID4) -> SemVer | None:
        """The highest version published so far, None if none ever was."""
        row = self.session.execute(
            select(
                ProjectContentVersionTable.major,
                ProjectContentVersionTable.minor,
                ProjectContentVersionTable.patch,
            )
            .where(
                ProjectContentVersionTable.project_id == project_id,
                ProjectContentVersionTable.major.is_not(None),
            )
            .order_by(
                ProjectContentVersionTable.major.desc(),
                ProjectContentVersionTable.minor.desc(),
                ProjectContentVersionTable.patch.desc(),
            )
            .limit(1)
        ).one_or_none()
        return None if row is None else (row.major, row.minor, row.patch)

    def _pending(self, content: ProjectContentTable) -> tuple[Bump, SemVer]:
        """What publishing the draft now would bump, and the version it would
        get. Numbered on from the highest published, not from the current
        one, so republishing restored content still moves forward."""
        published = content.published_version
        latest = self._latest_semver(content.project_id)
        if latest is None:
            return Bump.MAJOR, FIRST_VERSION
        if published is None:
            # Published before, but that version is gone: treat as major.
            return Bump.MAJOR, bumped(latest, Bump.MAJOR)
        bump = classify(published, content)
        return bump, bumped(latest, bump)

    def status(self, project_id: UUID4) -> ContentStatus:
        content = self._content(project_id)
        published = content.published_version
        bump, next_semver = self._pending(content)
        return ContentStatus(
            revision=content.revision,
            published=None
            if published is None
            else self._summary(published, published.id),
            # Recomputed on both sides rather than read from the stored hash,
            # which a library upgrade that adds a field would leave stale.
            has_unpublished_changes=published is None
            or content_hash(published) != content_hash(content),
            pending_bump=bump.label,
            next_semver=format_semver(next_semver),
            missing=missing_text(content),
        )

    # ==================== Writing ====================

    def _write(
        self,
        content: ProjectContentTable,
        kind: ContentVersionKind,
        label: str | None,
        user_id: UUID4 | None,
        digest: str,
    ) -> ProjectContentVersionTable:
        version_no = (
            self.session.execute(
                select(func.max(ProjectContentVersionTable.version_no)).where(
                    ProjectContentVersionTable.project_id == content.project_id
                )
            ).scalar_one()
            or 0
        ) + 1
        version = ProjectContentVersionTable(
            project_id=content.project_id,
            version_no=version_no,
            kind=kind,
            label=label,
            content_hash=digest,
            created_by_id=user_id,
        )
        copy_content(content, version)
        self.session.add(version)
        self.session.flush()
        return version

    def snapshot(
        self,
        project_id: UUID4,
        kind: ContentVersionKind,
        *,
        label: str | None = None,
        user_id: UUID4 | None = None,
    ) -> ProjectContentVersionTable:
        """A version holding the draft as it stands. Flushed, not committed.

        An automatic one reuses the newest version already holding the same
        content, so running ten tests on an unchanged draft writes one
        snapshot, and none at all when the draft is what was published. A
        manual or published one is always written: it records who saved what
        and when, which reusing an older row would lose.
        """
        content = self._content(project_id)
        digest = content_hash(content)
        if kind.is_automatic:
            existing = self.session.execute(
                select(ProjectContentVersionTable)
                .where(
                    ProjectContentVersionTable.project_id == project_id,
                    ProjectContentVersionTable.content_hash == digest,
                )
                .order_by(ProjectContentVersionTable.version_no.desc())
                .limit(1)
            ).scalar_one_or_none()
            if existing is not None:
                return existing
        version = self._write(content, kind, label, user_id, digest)
        if kind.is_automatic:
            self.prune(project_id)
        return version

    def save_version(
        self, project_id: UUID4, user_id: UUID4, label: str | None
    ) -> ContentVersionSummary:
        """Save the draft as a named version, without publishing it."""
        version = self.snapshot(
            project_id, ContentVersionKind.MANUAL, label=label, user_id=user_id
        )
        self.session.commit()
        return self._summary(version, self._content(project_id).published_version_id)

    def publish(
        self,
        project_id: UUID4,
        user_id: UUID4,
        label: str | None,
        bump: Bump | None = None,
    ) -> ContentVersionSummary:
        """Make the draft what respondents get, numbered by how far it is from
        the published version (app/db/versioning.py).

        `bump` raises the computed bump -- a rewording that changes what a
        question means is a patch to the diff -- and is refused with
        PublishError if it would lower it. Refused too while an offered
        language lacks text: the respondent would get the default language's
        in its place. Publishing a draft identical to the published version
        changes nothing.
        """
        content = self._content(project_id)
        if missing := missing_text(content):
            names = ", ".join(get_language_dict(lang)["name"] for lang in missing)
            raise PublishError(
                f"Translate everything into {names} before publishing.", missing
            )
        published = content.published_version
        if published is not None and content_hash(published) == content_hash(content):
            return self._summary(published, published.id)

        computed, _ = self._pending(content)
        if bump is not None and bump < computed:
            raise PublishError(
                f"These changes need at least a {computed.name.lower()} version; "
                f"a {bump.name.lower()} one would hide them.",
                {},
            )
        semver = bumped(
            self._latest_semver(project_id) or (0, 0, 0),
            max(computed, bump or Bump.NONE),
        )

        version = self.snapshot(
            project_id, ContentVersionKind.PUBLISH, label=label, user_id=user_id
        )
        version.major, version.minor, version.patch = semver
        content.published_version_id = version.id
        self.session.commit()
        return self._summary(version, version.id)

    def restore(
        self, project_id: UUID4, version_id: UUID4, user_id: UUID4
    ) -> ProjectContentTable:
        """Replace the draft with a version's content, languages and all.

        The draft it replaces is kept as a `restore` version first, so a
        restore can itself be undone. Publishes nothing.
        """
        version = self._version(project_id, version_id)
        self.snapshot(
            project_id,
            ContentVersionKind.RESTORE,
            label=f"Before restoring version {version.version_no}",
            user_id=user_id,
        )
        content = self._content(project_id)
        copy_content(version, content)
        self.session.commit()
        return content

    def prune(self, project_id: UUID4, keep: int = KEEP_AUTOMATIC_VERSIONS) -> int:
        """Delete automatic versions beyond the newest `keep`, except the
        published one and any an interview runs on. Returns how many went.

        `publish` and `manual` versions are never deleted.
        """
        published_id = self._content(project_id).published_version_id
        automatic = [k for k in ContentVersionKind if k.is_automatic]
        surplus = (
            select(ProjectContentVersionTable.id)
            .where(
                ProjectContentVersionTable.project_id == project_id,
                ProjectContentVersionTable.kind.in_(automatic),
            )
            .order_by(ProjectContentVersionTable.version_no.desc())
            .offset(keep)
        )
        candidates = [
            version_id
            for version_id in self.session.execute(surplus).scalars()
            if version_id != published_id
        ]
        if not candidates:
            return 0
        referenced = exists().where(
            InterviewTable.content_version_id == ProjectContentVersionTable.id
        )
        result = self.session.execute(
            delete(ProjectContentVersionTable)
            .where(ProjectContentVersionTable.id.in_(candidates), ~referenced)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount  # ty:ignore[unresolved-attribute]
