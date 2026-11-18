"""several conditions per question

Revision ID: b98bbe60532b
Revises: db2ac60f0f80
Create Date: 2026-11-18 09:29:00

"""

# NOTE: Don't manage the touch-last_updated triggers here. alembic/env.py
# uninstalls them before every run and reinstalls the full set from
# app/db/triggers.py after, so batch_alter_table on `project`, `testrun` etc.
# cannot lose or break them.
import json
import uuid
from collections.abc import Callable, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from pycrdt import Doc, Map

import app.db.types  # noqa: F401

# revision identifiers, used by Alembic.
revision: str = "b98bbe60532b"
down_revision: str | None = "db2ac60f0f80"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# A question carried at most one condition, `{"action", "conditions": [...]}`
# or null; it now carries a list of them, each `{"id", "action", "blocks":
# [...]}` (ainterviewer `feat: several conditions per question`). The lib reads
# only the new shape, so every stored copy is rewritten here:
#
# - `projectcontent.guide` and `projectcontentversion.guide`, the localized
#   guide. A rewritten version's `content_hash` is cleared: it only dedupes,
#   and NULL is what a version no new snapshot dedupes against.
# - `contentdoc.state` for the guide, a Yjs doc keeping each question's
#   conditions as JSON text under its `conditions` key. Rewritten in place,
#   encoded as `app.collab.guide_doc._set_value` writes it, rather than dropped
#   and re-seeded: the doc may hold edits that never reached the column.
# - `interview.interview_guide`, each interview's rendered copy.
# - `task` rows recording a condition evaluation (`evaluate_condition`), which
#   store the condition as content. The report counts firings by the
#   condition's id, read off this content.
#
# A condition's id is what ties these together, so it is one per project and
# question: the question's condition, wherever stored, gets the same id. A
# question carried only one, so that is the condition it has always been.
#
# An evaluation's id comes from its interview's copy: the question at the
# position the row records as its carrier (`context`, "<section>:<question>"
# in the copy's own, shuffled order), or, for rows written before the carrier
# was recorded, the one question in the copy whose condition is the content.
# Rows neither resolves are left without an id and the report skips them, as
# it skipped the unattributable ones before.
#
# Deliberately plain JSON, not the lib's models: this must keep doing the same
# thing however the models change later. Idempotent: a question whose
# conditions are already a list is left as is.

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
task = sa.table(
    "task",
    sa.column("id"),
    sa.column("interview_id"),
    sa.column("task", sa.Text()),
    sa.column("context", sa.Text()),
    sa.column("content", sa.Text()),
)

CONDITION_TASKS = ("evaluate_condition", "evaulate_condition")

Json = Any


def _load(raw: Json) -> Json:
    # JSON columns come back as text on some drivers.
    return json.loads(raw) if isinstance(raw, str) else raw


def _canonical(value: Json) -> str:
    # As `app.collab.guide_doc._set_value` encodes a doc's plain values.
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _questions(guide: Json) -> list[Json]:
    return [
        question
        for section in (guide or {}).get("question_sections") or []
        for question in section.get("questions") or []
    ]


# -- Upgrade -------------------------------------------------------------------


class _Ids:
    """The id of each project's question's condition, made on first ask."""

    def __init__(self) -> None:
        self._ids: dict[tuple[Any, str], str] = {}

    def __call__(self, project_id: Any, question_id: str | None) -> str:
        if question_id is None:
            return str(uuid.uuid4())
        key = (str(project_id), str(question_id))
        if key not in self._ids:
            self._ids[key] = str(uuid.uuid4())
        return self._ids[key]


def _upgrade_condition(old: Json, condition_id: str) -> Json:
    new = {key: value for key, value in old.items() if key != "conditions"}
    return {"id": condition_id, **new, "blocks": old.get("conditions") or []}


def _upgrade_value(
    value: Json, ids: _Ids, project_id: Any, question_id: str | None
) -> Json:
    """A question's `conditions` value in the new shape."""
    if isinstance(value, list):
        return value
    if not value:
        return []
    return [_upgrade_condition(value, ids(project_id, question_id))]


def _upgrade_guide(guide: Json, project_id: Any, ids: _Ids) -> bool:
    """Rewrite a guide's conditions in place. True if anything changed."""
    changed = False
    for question in _questions(guide):
        old = question.get("conditions")
        new = _upgrade_value(old, ids, project_id, question.get("id"))
        if new != old:
            question["conditions"] = new
            changed = True
    return changed


def _upgrade_table(bind, table, column: str, ids: _Ids, **on_change) -> None:
    for row in bind.execute(
        sa.select(table.c.id, table.c.project_id, table.c[column])
    ).all():
        guide = _load(row[2])
        if guide is not None and _upgrade_guide(guide, row.project_id, ids):
            bind.execute(
                table.update()
                .where(table.c.id == row.id)
                .values({column: guide, **on_change})
            )


def _rewrite_docs(bind, rewrite: Callable[[Any, str, Json], Json]) -> None:
    """Rewrite the `conditions` of every question in every guide doc.
    `rewrite(project_id, question_id, value)` gives the new value."""
    for row in bind.execute(
        sa.select(doc_table.c.id, doc_table.c.project_id, doc_table.c.state).where(
            # An enum on Postgres, which a text parameter does not compare to.
            sa.cast(doc_table.c.part, sa.Text()) == "GUIDE"
        )
    ).all():
        doc = Doc()
        doc.apply_update(row.state)
        questions = doc.get("questions", type=Map)
        changed = False
        with doc.transaction():
            for question_id, entry in questions.items():
                if not isinstance(entry, Map):
                    continue
                if (stored := entry.get("conditions")) is None:
                    continue
                old = json.loads(stored)
                new = rewrite(row.project_id, question_id, old)
                if new != old:
                    entry["conditions"] = _canonical(new)
                    changed = True
        if changed:
            bind.execute(
                doc_table.update()
                .where(doc_table.c.id == row.id)
                .values(state=doc.get_update())
            )


def _carrier(context: str | None) -> tuple[int, int] | None:
    section, _, question = (context or "").partition(":")
    try:
        return int(section), int(question)
    except ValueError:
        return None


def _evaluated_question(guide: Json, context: str | None, old: Json) -> Json | None:
    """The question of an interview's copy (still in the old shape) that an
    evaluation belongs to, or None if it cannot be told."""
    if (carrier := _carrier(context)) is not None:
        sections = guide.get("question_sections") or []
        try:
            return sections[carrier[0]]["questions"][carrier[1]]
        except (IndexError, KeyError, TypeError):
            return None
    matches = [
        question
        for question in _questions(guide)
        if isinstance(question.get("conditions"), dict)
        and _canonical(question["conditions"]) == _canonical(old)
    ]
    return matches[0] if len(matches) == 1 else None


def upgrade() -> None:
    bind = op.get_bind()
    ids = _Ids()

    # The draft first: its questions are the ones a condition id most
    # obviously belongs to. Every other copy then takes the same ids.
    _upgrade_table(bind, content, "guide", ids)
    _rewrite_docs(
        bind,
        lambda project_id, question_id, value: _upgrade_value(
            value, ids, project_id, question_id
        ),
    )
    _upgrade_table(bind, version, "guide", ids, content_hash=None)

    # Evaluations are resolved against their interview's copy before it is
    # rewritten, since a row without a carrier is matched by the old shape.
    copies = {
        row.id: (row.project_id, _load(row.interview_guide))
        for row in bind.execute(
            sa.select(
                interview.c.id, interview.c.project_id, interview.c.interview_guide
            )
        ).all()
    }

    unresolved = 0
    for row in bind.execute(
        sa.select(task.c.id, task.c.interview_id, task.c.context, task.c.content).where(
            task.c.task.in_(CONDITION_TASKS)
        )
    ).all():
        old = _load(row.content)
        if not isinstance(old, dict) or "blocks" in old:
            continue
        project_id, guide = copies.get(row.interview_id, (None, None))
        question = (
            _evaluated_question(guide, row.context, old) if guide is not None else None
        )
        if question is None or not question.get("conditions"):
            unresolved += 1
            new = {k: v for k, v in old.items() if k != "conditions"}
            new["blocks"] = old.get("conditions") or []
        else:
            new = _upgrade_condition(old, ids(project_id, question.get("id")))
        bind.execute(
            task.update().where(task.c.id == row.id).values(content=json.dumps(new))
        )

    for interview_id, (project_id, guide) in copies.items():
        if guide is not None and _upgrade_guide(guide, project_id, ids):
            bind.execute(
                interview.update()
                .where(interview.c.id == interview_id)
                .values(interview_guide=guide)
            )

    if unresolved:
        print(
            f"[{revision}] {unresolved} recorded condition evaluation(s) could not "
            "be tied to a question; left without a condition id"
        )


# -- Downgrade -----------------------------------------------------------------


def _downgrade_condition(new: Json) -> Json:
    old = {key: value for key, value in new.items() if key not in ("id", "blocks")}
    return {**old, "conditions": new.get("blocks") or []}


def _downgrade_value(value: Json, dropped: list[int]) -> Json:
    """A question's `conditions` value in the old shape: its first condition.
    Any after it cannot be kept, and are counted in `dropped`."""
    if not isinstance(value, list):
        return value
    if len(value) > 1:
        dropped[0] += len(value) - 1
    return _downgrade_condition(value[0]) if value else None


def downgrade() -> None:
    bind = op.get_bind()
    dropped = [0]

    tables: list[tuple[Any, str, dict]] = [
        (content, "guide", {}),
        (version, "guide", {"content_hash": None}),
        (interview, "interview_guide", {}),
    ]
    for table, column, on_change in tables:
        for row in bind.execute(sa.select(table.c.id, table.c[column])).all():
            guide = _load(row[1])
            changed = False
            for question in _questions(guide):
                old = question.get("conditions")
                new = _downgrade_value(old, dropped)
                if new != old:
                    question["conditions"] = new
                    changed = True
            if changed:
                bind.execute(
                    table.update()
                    .where(table.c.id == row.id)
                    .values({column: guide, **on_change})
                )

    # Not counted: a doc holds the same conditions as its column.
    _rewrite_docs(bind, lambda _p, _q, value: _downgrade_value(value, [0]))

    for row in bind.execute(
        sa.select(task.c.id, task.c.content).where(task.c.task.in_(CONDITION_TASKS))
    ).all():
        new = _load(row.content)
        if isinstance(new, dict) and "blocks" in new:
            bind.execute(
                task.update()
                .where(task.c.id == row.id)
                .values(content=json.dumps(_downgrade_condition(new)))
            )

    if dropped[0]:
        print(
            f"[{revision}] dropped {dropped[0]} condition(s) beyond the first on "
            "their question, which the older shape cannot hold"
        )
