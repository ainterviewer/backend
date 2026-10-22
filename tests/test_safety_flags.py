"""Tests that a safety-check intervention reaches the review queues.

The flag is written when the intervention message is stored, from the
`assess_security` task the check wrote just before it. What would go wrong
quietly: flagging synthetic tests (which would bury the queue), serving a
manual test's flag to the platform admins, pointing the
flag at the modal text instead of the answer that was assessed, losing the
decision's reasoning, never recording the respondent's override -- and
counting JSON ``null`` as an intervention, which is what every message the
app writes without one holds.
"""

import json
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ainterviewer.interfaces import SecurityIntervention, SecurityOverride
from ainterviewer.interview_guides import InterviewGuide
from ainterviewer.interview_guides.types import ConditionAction
from ainterviewer.types import MessageRole, MessageType
from app.db.repositories.interview import SECURITY_TASK, InterviewRepository
from app.db.repositories.reports import ReportRepository
from app.db.tables import (
    Base,
    InterviewTable,
    MessageReportTable,
    ProjectFolderTable,
    ProjectTable,
)
from app.db.types import InterviewType, ReportReason, ReportSource
from app.db.utils import messages_to_dataframe
from app.schemas.reports import MessageReportCreate

FOLDER = uuid.uuid4()
PROJECT = uuid.uuid4()
DISTRIBUTED = uuid.uuid4()
SYNTHETIC = uuid.uuid4()
MANUAL = uuid.uuid4()

ASSESSMENT = {
    "self_harm": {
        "reasoning": "Mentions wanting to hurt themselves",
        "probability": 0.9,
    },
    "distress": {"reasoning": "Calm otherwise", "probability": 0.1},
}


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(ProjectFolderTable(id=FOLDER, title="Folder"))
        session.add(
            ProjectTable(
                id=PROJECT, folder_id=FOLDER, title="Project", owner_id=uuid.uuid4()
            )
        )
        for interview_id, interview_type in (
            (DISTRIBUTED, InterviewType.DISTRIBUTED),
            (SYNTHETIC, InterviewType.SYNTHETIC_TEST),
        ):
            session.add(
                InterviewTable(
                    id=interview_id,
                    project_id=PROJECT,
                    interview_guide=InterviewGuide(),
                    type=interview_type,
                )
            )
        session.commit()
        yield session


@pytest.fixture
def interviews(session):
    return InterviewRepository(session)


def intervene(
    interviews: InterviewRepository,
    interview_id=DISTRIBUTED,
    respondent_override=True,
    override: SecurityOverride | None = None,
) -> None:
    """A question, an answer, its triggered assessment and the intervention,
    stored in the order the interview loop stores them."""
    interviews.insert_message(
        message_id=1,
        content="How are you?",
        role=MessageRole.ASSISTANT,
        interview_id=interview_id,
        project_id=PROJECT,
    )
    interviews.insert_message(
        message_id=2,
        content="I want to hurt myself",
        role=MessageRole.USER,
        interview_id=interview_id,
        project_id=PROJECT,
    )
    interviews.insert_task(
        message_id=2,
        task=SECURITY_TASK,
        reason="self_harm",
        response=json.dumps(ASSESSMENT),
        interview_id=interview_id,
        project_id=PROJECT,
    )
    interviews.insert_message(
        message_id=3,
        content="Our automated safety system has triggered an intervention.",
        role=MessageRole.ASSISTANT,
        can_answer=False,
        security_intervention=SecurityIntervention(
            action=ConditionAction.END_INTERVIEW,
            respondent_override=respondent_override,
        ),
        interview_id=interview_id,
        project_id=PROJECT,
    )
    if override is not None:
        interviews.insert_message(
            message_id=4,
            content=override,
            role=MessageRole.USER,
            message_type=MessageType.SECURITY_OVERRIDE,
            include_in_history=False,
            interview_id=interview_id,
            project_id=PROJECT,
        )


def flags(session) -> list[MessageReportTable]:
    return list(
        session.scalars(
            select(MessageReportTable).where(
                MessageReportTable.source == ReportSource.SAFETY_CHECK
            )
        )
    )


def test_an_intervention_is_flagged_on_the_answer_it_assessed(interviews, session):
    intervene(interviews)

    [flag] = flags(session)
    assert flag.message.content == "I want to hurt myself"
    assert flag.reason is None
    assert flag.safety is not None
    assert flag.safety.decision == "self_harm"
    assert flag.safety.probability == 0.9
    assert flag.safety.reasoning == "Mentions wanting to hurt themselves"
    assert flag.safety.action == ConditionAction.END_INTERVIEW
    assert flag.safety.override is None


def test_synthetic_tests_are_not_flagged(interviews, session):
    intervene(interviews, interview_id=SYNTHETIC)

    assert flags(session) == []


def test_a_manual_test_s_flag_reaches_the_project_but_not_the_platform(
    interviews, session
):
    session.add(
        InterviewTable(
            id=MANUAL,
            project_id=PROJECT,
            interview_guide=InterviewGuide(),
            type=InterviewType.MANUAL_TEST,
        )
    )
    session.commit()
    intervene(interviews, interview_id=MANUAL)
    reports = ReportRepository(session)
    reviewer = uuid.uuid4()

    [row] = reports.list_reports(user_id=reviewer, project_id=PROJECT)
    assert row.source == ReportSource.SAFETY_CHECK
    assert row.interview_type == InterviewType.MANUAL_TEST

    assert reports.list_admin_reports(user_id=reviewer) == []
    assert reports.unread_count(reviewer, track="admin") == 0

    # A respondent's report from the same test still goes to the platform: the
    # question it names is one real respondents will be asked.
    interviews.create_message_report(
        interview_id=MANUAL,
        project_id=PROJECT,
        report=MessageReportCreate(message_id=1, reason=ReportReason.OFFENSIVE),
    )
    [row] = reports.list_admin_reports(user_id=reviewer)
    assert row.source == ReportSource.RESPONDENT
    assert row.interview_type == InterviewType.MANUAL_TEST
    assert reports.unread_count(reviewer, track="admin") == 1


def test_a_distributed_flag_reaches_the_platform(interviews, session):
    intervene(interviews)
    reports = ReportRepository(session)
    reviewer = uuid.uuid4()

    [row] = reports.list_admin_reports(user_id=reviewer)
    assert row.interview_type == InterviewType.DISTRIBUTED
    assert reports.unread_count(reviewer, track="admin") == 1


@pytest.mark.parametrize("override", list(SecurityOverride))
def test_the_respondent_s_override_answer_lands_on_the_flag(
    interviews, session, override
):
    intervene(interviews, override=override)

    [flag] = flags(session)
    session.refresh(flag)
    assert flag.safety is not None
    assert flag.safety.override == override


def test_without_its_task_the_flag_names_the_intervention(interviews, session):
    interviews.insert_message(
        message_id=1,
        content="Our automated safety system has triggered an intervention.",
        role=MessageRole.ASSISTANT,
        interview_id=DISTRIBUTED,
        project_id=PROJECT,
        security_intervention=SecurityIntervention(
            action=ConditionAction.SKIP_PROBES, respondent_override=False
        ),
    )

    [flag] = flags(session)
    assert flag.safety is not None
    assert flag.safety.decision == "unknown"
    assert flag.message.message_id == 1


def test_flags_are_served_in_the_queue_with_their_decision(interviews, session):
    intervene(interviews)

    [row] = ReportRepository(session).list_reports(
        user_id=uuid.uuid4(), project_id=PROJECT
    )
    assert row.source == ReportSource.SAFETY_CHECK
    assert row.question == "I want to hurt myself"
    assert row.safety is not None
    assert row.safety.decision == "self_harm"


def test_interventions_are_counted_on_every_interview_type(interviews, session):
    intervene(interviews, interview_id=DISTRIBUTED)
    intervene(interviews, interview_id=SYNTHETIC)

    summaries, _ = interviews.get_interviews(
        PROJECT, interview_types=list(InterviewType)
    )
    counts = {summary.id: summary.n_interventions for summary in summaries}
    assert counts == {DISTRIBUTED: 1, SYNTHETIC: 1}

    # The messages without one hold JSON null, which must not count.
    reports = {summary.id: summary.n_reports for summary in summaries}
    assert reports == {DISTRIBUTED: 1, SYNTHETIC: 0}


def test_the_intervened_filter_and_facet(interviews, session):
    intervene(interviews, interview_id=SYNTHETIC)
    interviews.insert_message(
        message_id=1,
        content="Hello",
        role=MessageRole.ASSISTANT,
        interview_id=DISTRIBUTED,
        project_id=PROJECT,
    )

    every_type = list(InterviewType)
    flagged, total = interviews.get_interviews(
        PROJECT, interview_types=every_type, intervened=True
    )
    assert [summary.id for summary in flagged] == [SYNTHETIC]
    assert total == 1

    calm, _ = interviews.get_interviews(
        PROJECT, interview_types=every_type, intervened=False
    )
    assert [summary.id for summary in calm] == [DISTRIBUTED]

    facets = interviews.get_interview_facets(PROJECT, interview_types=every_type)
    assert facets["intervened"] == {"true": 1, "false": 1}


def test_a_flag_exports_its_decision_as_the_reason(interviews, session):
    intervene(interviews)

    transcript = interviews.get_interview(PROJECT, DISTRIBUTED, full=True)
    frame = messages_to_dataframe(transcript.messages)
    assert "safety:self_harm" in frame["report_reasons"].to_list()
