"""flag safety-check interventions for review

Safety-check interventions join respondent reports in `message_report`, so they
reach the same review queues, tracks and badges. `source` says which a row is;
a safety flag has no `reason` (now nullable) and carries `safety` instead: the
decision that triggered, its reasoning and probability, the action, and the
respondent's override answer if they gave one. See `MessageReportTable`.

Interventions stored before this revision are backfilled, on distributed
interviews only, as the app writes them from here on. Their decision comes off
the latest triggered `assess_security` task at or before the intervention, and
the flag names the answer that task assessed.

The downgrade deletes the safety flags -- they cannot be told apart from a
report once `source` is gone -- and makes `reason` required again.

Revision ID: 5e1f0b7c2a94
Revises: cc6e75238415
Create Date: 2026-10-16 12:00:00.000000

"""

import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from ainterviewer.utils import now

# revision identifiers, used by Alembic.
revision: str = "5e1f0b7c2a94"
down_revision: str | None = "cc6e75238415"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# `SQLEnum` stores member names. Frozen here rather than read off the enums.
REASON = sa.Enum(
    "INAPPROPRIATE", "OFFENSIVE", "IRRELEVANT", "OTHER", name="reportreason"
)
SOURCE = sa.Enum("RESPONDENT", "SAFETY_CHECK", name="reportsource")

message = sa.table(
    "message",
    sa.column("id", sa.Uuid()),
    sa.column("interview_id", sa.Uuid()),
    sa.column("project_id", sa.Uuid()),
    sa.column("message_id", sa.Integer()),
    sa.column("role", sa.String()),
    sa.column("message_type", sa.String()),
    sa.column("content", sa.Text()),
    sa.column("created_at", sa.DateTime()),
    sa.column("security_intervention", sa.JSON()),
)
interview = sa.table("interview", sa.column("id", sa.Uuid()), sa.column("type"))
task = sa.table(
    "task",
    sa.column("interview_id", sa.Uuid()),
    sa.column("message_id", sa.Integer()),
    sa.column("task", sa.String()),
    sa.column("reason", sa.Text()),
    sa.column("response", sa.Text()),
    sa.column("created_at", sa.DateTime()),
)
report = sa.table(
    "message_report",
    sa.column("id", sa.Uuid()),
    sa.column("message_id", sa.Uuid()),
    sa.column("interview_id", sa.Uuid()),
    sa.column("project_id", sa.Uuid()),
    sa.column("source", SOURCE),
    sa.column("safety", sa.JSON()),
    sa.column("created_at", sa.DateTime()),
    sa.column("updated_at", sa.DateTime()),
    sa.column("status", sa.String()),
    sa.column("admin_status", sa.String()),
)


def _loads(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _backfill() -> None:
    bind = op.get_bind()
    interventions = bind.execute(
        sa.select(message)
        .join(interview, interview.c.id == message.c.interview_id)
        .where(
            message.c.security_intervention.is_not(None),
            sa.cast(message.c.security_intervention, sa.Text) != "null",
            interview.c.type == "DISTRIBUTED",
        )
        .order_by(message.c.interview_id, message.c.message_id)
    ).all()

    for row in interventions:
        intervention = _loads(row.security_intervention)
        if not isinstance(intervention, dict):
            continue

        assessed = bind.execute(
            sa.select(task.c.message_id, task.c.reason, task.c.response)
            .where(
                task.c.interview_id == row.interview_id,
                task.c.task == "assess_security",
                task.c.reason.is_not(None),
                task.c.created_at <= row.created_at,
            )
            .order_by(task.c.created_at.desc())
            .limit(1)
        ).first()

        decision = assessed.reason if assessed else "unknown"
        evaluation = (_loads(assessed.response) or {}) if assessed else {}
        evaluation = evaluation.get(decision) if isinstance(evaluation, dict) else None
        evaluation = evaluation if isinstance(evaluation, dict) else {}

        flagged_id = row.id
        if assessed is not None:
            flagged_id = (
                bind.scalar(
                    sa.select(message.c.id).where(
                        message.c.interview_id == row.interview_id,
                        message.c.message_id == assessed.message_id,
                        message.c.role == "USER",
                    )
                )
                or row.id
            )

        override = bind.scalar(
            sa.select(message.c.content).where(
                message.c.interview_id == row.interview_id,
                message.c.message_id == row.message_id + 1,
                message.c.message_type == "SECURITY_OVERRIDE",
            )
        )

        stamp = row.created_at or now()
        bind.execute(
            sa.insert(report).values(
                id=uuid.uuid4(),
                message_id=flagged_id,
                interview_id=row.interview_id,
                project_id=row.project_id,
                source="SAFETY_CHECK",
                safety={
                    "decision": decision,
                    "probability": evaluation.get("probability"),
                    "reasoning": evaluation.get("reasoning"),
                    "action": intervention.get("action"),
                    "respondent_override": bool(
                        intervention.get("respondent_override")
                    ),
                    "override": override,
                },
                created_at=stamp,
                updated_at=stamp,
                status="OPEN",
                admin_status="OPEN",
            )
        )


def upgrade() -> None:
    with op.batch_alter_table("message_report", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("source", SOURCE, nullable=False, server_default="RESPONDENT")
        )
        batch_op.add_column(sa.Column("safety", sa.JSON(), nullable=True))
        batch_op.alter_column("reason", existing_type=REASON, nullable=True)

    _backfill()


def downgrade() -> None:
    # Alembic runs with foreign keys off, so the read rows would not cascade.
    flags = sa.select(report.c.id).where(report.c.source == "SAFETY_CHECK")
    read = sa.table("message_report_read", sa.column("report_id", sa.Uuid()))
    op.execute(sa.delete(read).where(read.c.report_id.in_(flags)))
    op.execute(sa.delete(report).where(report.c.source == "SAFETY_CHECK"))

    with op.batch_alter_table("message_report", schema=None) as batch_op:
        batch_op.alter_column("reason", existing_type=REASON, nullable=False)
        batch_op.drop_column("safety")
        batch_op.drop_column("source")
