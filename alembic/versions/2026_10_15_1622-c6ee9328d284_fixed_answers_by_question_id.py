"""fixed answers by question id

Revision ID: c6ee9328d284
Revises: e82825da6890
Create Date: 2026-10-15 16:22:22.765321

"""

# NOTE: Don't manage the touch-last_updated triggers here. alembic/env.py
# uninstalls them before every run and reinstalls the full set from
# app/db/triggers.py after, so batch_alter_table on `project`, `testrun` etc.
# cannot lose or break them.
import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

import app.db.types  # noqa: F401

# revision identifiers, used by Alembic.
revision: str = "c6ee9328d284"
down_revision: str | None = "e82825da6890"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# `testsetup.fixed_answers` was a flat list, one answer per answerable
# question in guide order, and the synthetic interviewee spent them in the
# order questions were asked. Reordering the guide, adding or removing a
# question, a shuffled section or a condition that skips one all moved every
# later answer onto another question without a word. It becomes
# `{question_id: answer}`.
#
# The lists are mapped the way the setup page showed them: the n-th answer
# belongs to the n-th answerable question of the project's draft guide, and
# answers past the last one (from questions removed since) are dropped, as
# the page already dropped them. Blank answers are left out. Re-running is
# safe: a value that is already a map is skipped.

testsetup = sa.table(
    "testsetup",
    sa.column("id"),
    sa.column("project_id"),
    sa.column("fixed_answers", sa.JSON()),
)
projectcontent = sa.table(
    "projectcontent", sa.column("project_id"), sa.column("guide", sa.JSON())
)


def _load(value: Any) -> Any:
    """JSON comes back as text on SQLite, decoded on Postgres."""
    return json.loads(value) if isinstance(value, str) else value


def _answerable_ids(guide: Any) -> list[str]:
    guide = _load(guide) or {}
    return [
        str(question["id"])
        for section in guide.get("question_sections") or []
        for question in section.get("questions") or []
        if question.get("can_answer", True) is True and question.get("id")
    ]


def _guides(bind) -> dict[Any, list[str]]:
    rows = bind.execute(sa.select(projectcontent.c.project_id, projectcontent.c.guide))
    return {project_id: _answerable_ids(guide) for project_id, guide in rows}


def upgrade() -> None:
    bind = op.get_bind()
    guides = _guides(bind)
    rows = bind.execute(
        sa.select(testsetup.c.id, testsetup.c.project_id, testsetup.c.fixed_answers)
    ).all()
    for id_, project_id, value in rows:
        answers = _load(value)
        if not isinstance(answers, list):
            continue
        ids = guides.get(project_id, [])
        mapped = {
            question_id: answer.strip()
            for question_id, answer in zip(ids, answers, strict=False)
            if isinstance(answer, str) and answer.strip()
        }
        bind.execute(
            sa.update(testsetup)
            .where(testsetup.c.id == id_)
            .values(fixed_answers=mapped or None)
        )


def downgrade() -> None:
    bind = op.get_bind()
    guides = _guides(bind)
    rows = bind.execute(
        sa.select(testsetup.c.id, testsetup.c.project_id, testsetup.c.fixed_answers)
    ).all()
    for id_, project_id, value in rows:
        answers = _load(value)
        if not isinstance(answers, dict):
            continue
        ids = guides.get(project_id, [])
        listed = [answers.get(question_id, "") for question_id in ids]
        bind.execute(
            sa.update(testsetup)
            .where(testsetup.c.id == id_)
            .values(fixed_answers=listed if any(listed) else None)
        )
