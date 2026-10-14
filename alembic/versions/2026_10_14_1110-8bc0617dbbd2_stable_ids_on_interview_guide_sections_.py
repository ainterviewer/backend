"""stable ids on interview guide sections and questions

Revision ID: 8bc0617dbbd2
Revises: 6b1d0e7a9c35
Create Date: 2026-10-14 11:10:04.701140

"""

# NOTE: Don't manage the touch-last_updated triggers here. alembic/env.py
# uninstalls them before every run and reinstalls the full set from
# app/db/triggers.py after, so batch_alter_table on `project`, `testrun` etc.
# cannot lose or break them.
import json
import uuid
from collections import defaultdict
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.types  # noqa: F401

# revision identifiers, used by Alembic.
revision: str = "8bc0617dbbd2"
down_revision: str | None = "6b1d0e7a9c35"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Guide sections and questions get a persisted `id`, and conditions and
# references name the question they read by `question_id` instead of only by
# position (ainterviewer `feat!: stable ids ...`). The lib now *requires* that
# id and no longer reads the old `{section, question}` condition shape, so
# every stored guide is rewritten here rather than upgraded on load.
#
# Deliberately plain JSON, not the lib's models: this must keep doing the same
# thing however the models change later.
#
# - A condition's or reference's id is taken from the question at the position
#   it names. For a shuffled interview copy that is the question the old
#   runtime actually read (it looked answers up by position in asked order),
#   so past interviews keep the record of what happened.
# - A condition naming a position with no question there could never be
#   evaluated; it is dropped and counted. A dangling reference cannot be
#   dropped -- the main question's `{}` placeholders must match the reference
#   count -- so it stops the migration for a person to resolve.
# - A project's other localizations take the default localization's ids by
#   position when their structure (questions per section) matches it, so a
#   question has one id in every language. Translation copies the guide, so
#   that is the usual case.
# - `evaluate_condition` task rows (also spelled `evaulate_condition` before a
#   lib fix) store the conditions JSON; they get the same shape, with an id
#   where their interview's copy resolves one. Nothing parses them back.
#
# Idempotent: anything already carrying an id or `question_index` is left as is.

localization = sa.table(
    "projectlocalization",
    sa.column("id"),
    sa.column("project_id"),
    sa.column("is_default", sa.Boolean()),
    sa.column("interview_guide", sa.Text()),
)
interview = sa.table(
    "interview", sa.column("id"), sa.column("interview_guide", sa.Text())
)
task = sa.table(
    "task",
    sa.column("id"),
    sa.column("interview_id"),
    sa.column("task", sa.Text()),
    sa.column("content", sa.Text()),
)

CONDITION_TASKS = ("evaluate_condition", "evaulate_condition")

type Position = tuple[int, int]


def _sections(guide: dict) -> list[dict]:
    return guide.get("question_sections") or []


def _questions(section: dict) -> list[dict]:
    return section.get("questions") or []


def _structure(guide: dict) -> list[int]:
    return [len(_questions(section)) for section in _sections(guide)]


def _assign_ids(guide: dict, like: dict | None = None) -> None:
    """Give every section and question an id, copying `like`'s by position
    when the two guides have the same structure."""
    template = (
        _sections(like)
        if like is not None and _structure(like) == _structure(guide)
        else None
    )
    for n_section, section in enumerate(_sections(guide)):
        source = template[n_section] if template else None
        section.setdefault("id", source["id"] if source else str(uuid.uuid4()))
        for n_question, question in enumerate(_questions(section)):
            question.setdefault(
                "id",
                _questions(source)[n_question]["id"] if source else str(uuid.uuid4()),
            )


def _position_ids(guide: dict) -> dict[Position, str]:
    return {
        (n_section, n_question): question["id"]
        for n_section, section in enumerate(_sections(guide))
        for n_question, question in enumerate(_questions(section))
    }


def _upgrade_context(context: dict, ids: dict[Position, str]) -> bool:
    """Move a condition's question context onto the new shape. False if it
    names a position with no question."""
    if "question_index" not in context:
        context["question_index"] = [context.pop("section"), context.pop("question")]
    if "question_id" not in context:
        question_id = ids.get(tuple(context["question_index"]))
        if question_id is None:
            return False
        context["question_id"] = question_id
    return True


def _upgrade_refs(guide: dict, where: str) -> int:
    """Upgrade every condition and reference in the guide. Returns the number
    of dangling conditions dropped."""
    ids = _position_ids(guide)
    dropped = 0
    for section in _sections(guide):
        for question in _questions(section):
            if conditions := question.get("conditions"):
                listed = conditions.get("conditions") or []
                kept = [
                    c for c in listed if _upgrade_context(c["question_context"], ids)
                ]
                dropped += len(listed) - len(kept)
                if kept:
                    # The last condition combines with nothing.
                    kept[-1]["combine_next"] = None
                    conditions["conditions"] = kept
                else:
                    question["conditions"] = None

            for reference in question.get("references") or []:
                if "question_id" in reference:
                    continue
                question_id = ids.get(tuple(reference["question_index"]))
                if question_id is None:
                    raise RuntimeError(
                        f"[{revision}] {where}: a reference names position "
                        f"{reference['question_index']}, which holds no question. "
                        "Fix the guide by hand and re-run."
                    )
                reference["question_id"] = question_id
    return dropped


def _load(raw: str | None) -> dict | None:
    return json.loads(raw) if raw else None


def upgrade() -> None:
    bind = op.get_bind()
    dropped: dict[str, int] = defaultdict(int)

    rows = bind.execute(
        sa.select(
            localization.c.id,
            localization.c.project_id,
            localization.c.is_default,
            localization.c.interview_guide,
        )
    ).all()
    by_project = defaultdict(list)
    for row in rows:
        by_project[row.project_id].append(row)

    for project_rows in by_project.values():
        # The default first, so the others can copy its ids.
        project_rows.sort(key=lambda row: not row.is_default)
        default_guide = None
        for row in project_rows:
            guide = _load(row.interview_guide)
            if guide is None:
                continue
            _assign_ids(guide, like=default_guide)
            if row.is_default:
                default_guide = guide
            dropped["localization"] += _upgrade_refs(
                guide, f"projectlocalization {row.id}"
            )
            bind.execute(
                localization.update()
                .where(localization.c.id == row.id)
                .values(interview_guide=json.dumps(guide))
            )

    interview_ids: dict[str, dict[Position, str]] = {}
    for row in bind.execute(
        sa.select(interview.c.id, interview.c.interview_guide)
    ).all():
        guide = _load(row.interview_guide)
        if guide is None:
            continue
        _assign_ids(guide)
        dropped["interview"] += _upgrade_refs(guide, f"interview {row.id}")
        interview_ids[row.id] = _position_ids(guide)
        bind.execute(
            interview.update()
            .where(interview.c.id == row.id)
            .values(interview_guide=json.dumps(guide))
        )

    unresolved_tasks = 0
    for row in bind.execute(
        sa.select(task.c.id, task.c.interview_id, task.c.content).where(
            task.c.task.in_(CONDITION_TASKS)
        )
    ).all():
        content = _load(row.content)
        if content is None:
            continue
        ids = interview_ids.get(row.interview_id, {})
        for condition in content.get("conditions") or []:
            if not _upgrade_context(condition["question_context"], ids):
                unresolved_tasks += 1
        bind.execute(
            task.update().where(task.c.id == row.id).values(content=json.dumps(content))
        )

    for where, count in dropped.items():
        if count:
            print(
                f"[{revision}] dropped {count} {where} condition(s) naming no question"
            )
    if unresolved_tasks:
        print(
            f"[{revision}] {unresolved_tasks} recorded condition evaluation(s) name no "
            "question in their interview; left with a position and no question_id"
        )


def _downgrade_refs(guide: dict) -> None:
    for section in _sections(guide):
        for question in _questions(section):
            for condition in (question.get("conditions") or {}).get("conditions") or []:
                _downgrade_context(condition["question_context"])
            for reference in question.get("references") or []:
                reference.pop("question_id", None)


def _downgrade_context(context: dict) -> None:
    context.pop("question_id", None)
    if "question_index" in context:
        context["section"], context["question"] = context.pop("question_index")


def downgrade() -> None:
    # Section and question ids stay: the older lib ignores fields it does not
    # know. Dropped dangling conditions are not restored.
    bind = op.get_bind()
    for table in (localization, interview):
        for row in bind.execute(sa.select(table.c.id, table.c.interview_guide)).all():
            if (guide := _load(row.interview_guide)) is None:
                continue
            _downgrade_refs(guide)
            bind.execute(
                table.update()
                .where(table.c.id == row.id)
                .values(interview_guide=json.dumps(guide))
            )

    for row in bind.execute(
        sa.select(task.c.id, task.c.content).where(task.c.task.in_(CONDITION_TASKS))
    ).all():
        if (content := _load(row.content)) is None:
            continue
        for condition in content.get("conditions") or []:
            _downgrade_context(condition["question_context"])
        bind.execute(
            task.update().where(task.c.id == row.id).values(content=json.dumps(content))
        )
