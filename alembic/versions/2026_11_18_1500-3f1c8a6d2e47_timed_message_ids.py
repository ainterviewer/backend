"""timed message ids

Revision ID: 3f1c8a6d2e47
Revises: b98bbe60532b
Create Date: 2026-11-18 15:00:00

"""

# NOTE: Don't manage the touch-last_updated triggers here. alembic/env.py
# uninstalls them before every run and reinstalls the full set from
# app/db/triggers.py after, so batch_alter_table on `project`, `testrun` etc.
# cannot lose or break them.
import json
import uuid
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from pycrdt import Array, Doc, Map

import app.db.types  # noqa: F401

# revision identifiers, used by Alembic.
revision: str = "3f1c8a6d2e47"
down_revision: str | None = "b98bbe60532b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# A guide's timed messages get a persisted `id`, and a timed message row
# records the id of the one it sent (`message.timed_message_id`), which a
# resumed interview reads to not send it again (ainterviewer `feat: give timed
# messages ids`). That id is also what marks a row as a timed message, so the
# `message.timed` flag goes. The lib makes up an id for a timed message stored
# without one, a new one on every load, so every stored copy is given one
# here:
#
# - `projectcontent.guide` and `projectcontentversion.guide`, the localized
#   guide. A rewritten version's `content_hash` is cleared: it only dedupes,
#   and NULL is what a version no new snapshot dedupes against.
# - `contentdoc.state` for the guide, a Yjs doc keeping each timed message as a
#   map in the `timed_messages` array of its `guide` root, the id as JSON text
#   the way `app.collab.guide_doc._set_value` writes it. Rewritten in place
#   rather than dropped and re-seeded: the doc may hold edits that never
#   reached the column.
# - `interview.interview_guide`, each interview's rendered copy.
#
# One id per project and position: a project's copies share the draft's ids
# where they have a timed message at the same place. An interview copy may
# have lost the ones it had sent (the old runtime dropped them from the guide
# it saved back), so it is matched by `time` instead, falling back to a new id.
#
# Every row flagged `timed` gets an id: its interview copy's timed message
# with the same text, or else a new id that names no message in the guide.
# Either way the row still reads as a timed message -- which keeps a resumed
# interview counting its messages right -- but one given a new id may have its
# message sent again if the interview is resumed, which was accepted over
# guessing harder.
#
# Deliberately plain JSON, not the lib's models: this must keep doing the same
# thing however the models change later. Idempotent: a timed message that has
# an id keeps it.

content = sa.table(
    "projectcontent",
    sa.column("id"),
    sa.column("project_id"),
    sa.column("guide", sa.JSON()),
)
version = sa.table(
    "projectcontentversion",
    sa.column("id"),
    sa.column("project_id"),
    sa.column("guide", sa.JSON()),
    sa.column("content_hash", sa.Text()),
)
doc_table = sa.table(
    "contentdoc",
    sa.column("id"),
    sa.column("project_id"),
    sa.column("part", sa.Text()),
    sa.column("state", sa.LargeBinary()),
)
interview = sa.table(
    "interview",
    sa.column("id"),
    sa.column("project_id"),
    sa.column("interview_guide", sa.JSON()),
)

message_table = sa.table(
    "message",
    sa.column("id"),
    sa.column("interview_id"),
    sa.column("content", sa.Text()),
    sa.column("timed", sa.Boolean()),
    sa.column("timed_message_id", sa.Uuid()),
)

Json = Any


def _load(raw: Json) -> Json:
    # JSON columns come back as text on some drivers.
    return json.loads(raw) if isinstance(raw, str) else raw


def _canonical(value: Json) -> str:
    # As `app.collab.guide_doc._set_value` encodes a doc's plain values.
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _timed(guide: Json) -> list[Json]:
    return (guide or {}).get("timed_messages") or []


class _Ids:
    """The id of each project's timed message, by position, made on first ask;
    and the time each was given at, for copies matched by time."""

    def __init__(self) -> None:
        self._ids: dict[tuple[str, int], str] = {}
        self._times: dict[tuple[str, Any], str] = {}

    def at(self, project_id: Any, n: int, time: Any = None) -> str:
        key = (str(project_id), n)
        if key not in self._ids:
            self._ids[key] = str(uuid.uuid4())
        if time is not None:
            self._times.setdefault((str(project_id), time), self._ids[key])
        return self._ids[key]

    def by_time(self, project_id: Any, time: Any) -> str:
        return self._times.get((str(project_id), time)) or str(uuid.uuid4())


def _by_position(guide: Json, project_id: Any, ids: _Ids) -> bool:
    changed = False
    for n, message in enumerate(_timed(guide)):
        if "id" not in message:
            message["id"] = ids.at(project_id, n, message.get("time"))
            changed = True
    return changed


def _by_time(guide: Json, project_id: Any, ids: _Ids) -> bool:
    changed = False
    taken: set[str] = set()
    for message in _timed(guide):
        if "id" not in message:
            message_id = ids.by_time(project_id, message.get("time"))
            # Two at the same time in one copy cannot share an id.
            if message_id in taken:
                message_id = str(uuid.uuid4())
            message["id"] = message_id
            changed = True
        taken.add(message["id"])
    return changed


def _upgrade_table(bind, table, column: str, ids: _Ids, give, **on_change) -> None:
    for row in bind.execute(
        sa.select(table.c.id, table.c.project_id, table.c[column])
    ).all():
        guide = _load(row[2])
        if guide is not None and give(guide, row.project_id, ids):
            bind.execute(
                table.update()
                .where(table.c.id == row.id)
                .values({column: guide, **on_change})
            )


def _guide_docs(bind):
    return bind.execute(
        sa.select(doc_table.c.id, doc_table.c.project_id, doc_table.c.state).where(
            # An enum on Postgres, which a text parameter does not compare to.
            sa.cast(doc_table.c.part, sa.Text()) == "GUIDE"
        )
    ).all()


def _rewrite_docs(bind, rewrite) -> None:
    """`rewrite(project_id, n, entry)` edits the n-th timed message's map in
    place, returning True if it changed it."""
    for row in _guide_docs(bind):
        doc = Doc()
        doc.apply_update(row.state)
        top = doc.get("guide", type=Map)
        timed = top.get("timed_messages")
        if not isinstance(timed, Array):
            continue
        changed = False
        with doc.transaction():
            for n, entry in enumerate(timed):
                if isinstance(entry, Map) and rewrite(row.project_id, n, entry):
                    changed = True
        if changed:
            bind.execute(
                doc_table.update()
                .where(doc_table.c.id == row.id)
                .values(state=doc.get_update())
            )


def _doc_time(entry: Map) -> Any:
    stored = entry.get("time")
    return json.loads(stored) if isinstance(stored, str) else stored


def upgrade() -> None:
    op.add_column("message", sa.Column("timed_message_id", sa.Uuid(), nullable=True))

    bind = op.get_bind()
    ids = _Ids()

    # The draft first: its timed messages are the ones an id most obviously
    # belongs to. Every other copy then takes the same ids.
    _upgrade_table(bind, content, "guide", ids, _by_position)

    def give_doc(project_id: Any, n: int, entry: Map) -> bool:
        if "id" in entry:
            return False
        entry["id"] = _canonical(ids.at(project_id, n, _doc_time(entry)))
        return True

    _rewrite_docs(bind, give_doc)
    _upgrade_table(bind, version, "guide", ids, _by_position, content_hash=None)
    _upgrade_table(bind, interview, "interview_guide", ids, _by_time)

    copies = {
        row.id: _timed(_load(row.interview_guide))
        for row in bind.execute(
            sa.select(interview.c.id, interview.c.interview_guide)
        ).all()
    }
    for row in bind.execute(
        sa.select(
            message_table.c.id, message_table.c.interview_id, message_table.c.content
        ).where(message_table.c.timed.is_(True))
    ).all():
        same_text = [
            timed["id"]
            for timed in copies.get(row.interview_id, [])
            if timed.get("message") == row.content
        ]
        bind.execute(
            message_table.update()
            .where(message_table.c.id == row.id)
            .values(
                timed_message_id=uuid.UUID(same_text[0])
                if len(same_text) == 1
                else uuid.uuid4()
            )
        )

    with op.batch_alter_table("message") as batch:
        batch.drop_column("timed")


def downgrade() -> None:
    with op.batch_alter_table("message") as batch:
        batch.add_column(
            sa.Column("timed", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    bind = op.get_bind()
    bind.execute(
        message_table.update()
        .where(message_table.c.timed_message_id.is_not(None))
        .values(timed=True)
    )

    tables: list[tuple[Any, str, dict]] = [
        (content, "guide", {}),
        (version, "guide", {"content_hash": None}),
        (interview, "interview_guide", {}),
    ]
    for table, column, on_change in tables:
        for row in bind.execute(sa.select(table.c.id, table.c[column])).all():
            guide = _load(row[1])
            changed = False
            for message in _timed(guide):
                if message.pop("id", None) is not None:
                    changed = True
            if changed:
                bind.execute(
                    table.update()
                    .where(table.c.id == row.id)
                    .values({column: guide, **on_change})
                )

    def strip_doc(_project_id: Any, _n: int, entry: Map) -> bool:
        if "id" not in entry:
            return False
        del entry["id"]
        return True

    _rewrite_docs(bind, strip_doc)

    with op.batch_alter_table("message") as batch:
        batch.drop_column("timed_message_id")
