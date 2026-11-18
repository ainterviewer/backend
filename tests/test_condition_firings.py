"""The report's count of how often each condition was evaluated and fired.

Counted per condition, by the id the interview records with each evaluation,
so that a question with several conditions -- skip the section on one answer,
skip the probes on another -- gets a count for each. What would go wrong
quietly: one condition's firings showing on its sibling, an evaluation from
another project or outside the cohort counting, or one the migration could not
tie to a condition counting anywhere.
"""

import json
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ainterviewer.interview_guides import InterviewGuide
from ainterviewer.interview_guides.conditions import (
    Condition,
    ConditionBlock,
    ConditionEvaluation,
    QuestionContext,
)
from ainterviewer.interview_guides.types import ConditionAction
from app.api.dashboard.analysis.cohort import interview_cohort
from app.api.dashboard.analysis.report import (
    CONDITION_TASK,
    _condition_counts,
    _condition_firings,
)
from app.db.repositories.interview import InterviewRepository
from app.db.tables import Base, InterviewTable, ProjectFolderTable, ProjectTable
from app.types import InterviewType

FOLDER = uuid.uuid4()
PROJECT, OTHER_PROJECT = uuid.uuid4(), uuid.uuid4()
IN_COHORT, OUT_OF_COHORT, ELSEWHERE = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def _condition(action: ConditionAction) -> Condition:
    return Condition(
        action=action,
        blocks=[
            ConditionBlock(
                question_context=QuestionContext(
                    question_index=(0, 0), question_id=uuid.uuid4()
                ),
                evaluation=[ConditionEvaluation(trigger_value="Nej")],
            )
        ],
    )


SKIP_SECTION = _condition(ConditionAction.SKIP_SECTION)
SKIP_PROBES = _condition(ConditionAction.SKIP_PROBES)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(ProjectFolderTable(id=FOLDER, title="Folder"))
        for project_id in (PROJECT, OTHER_PROJECT):
            session.add(
                ProjectTable(
                    id=project_id,
                    folder_id=FOLDER,
                    title=f"Project {project_id}",
                    owner_id=uuid.uuid4(),
                )
            )
        for interview_id, project_id, interview_type in (
            (IN_COHORT, PROJECT, InterviewType.DISTRIBUTED),
            (OUT_OF_COHORT, PROJECT, InterviewType.SYNTHETIC_TEST),
            (ELSEWHERE, OTHER_PROJECT, InterviewType.DISTRIBUTED),
        ):
            session.add(
                InterviewTable(
                    id=interview_id,
                    project_id=project_id,
                    interview_guide=InterviewGuide(),
                    type=interview_type,
                )
            )
        session.commit()
        yield session


def _evaluated(
    session, condition: Condition | str, fired: bool, interview_id=IN_COHORT
) -> None:
    InterviewRepository(session).insert_task(
        message_id=1,
        task=CONDITION_TASK,
        content=condition
        if isinstance(condition, str)
        else condition.model_dump_json(),
        response=str(fired),
        context="0:1",
        interview_id=interview_id,
        project_id=OTHER_PROJECT if interview_id == ELSEWHERE else PROJECT,
    )


def _counts(session) -> dict[uuid.UUID, tuple[int, int]]:
    cohort = interview_cohort(
        [InterviewTable.id.label("id")],
        [
            InterviewTable.project_id == PROJECT,
            InterviewTable.type == InterviewType.DISTRIBUTED,
        ],
    )
    firings = _condition_firings(session, PROJECT, cohort)
    return {
        counts.condition_id: (counts.n_evaluated, counts.n_fired)
        for counts in _condition_counts([SKIP_SECTION, SKIP_PROBES], firings)
    }


def test_each_condition_on_a_question_is_counted_apart(session):
    _evaluated(session, SKIP_SECTION, fired=False)
    _evaluated(session, SKIP_PROBES, fired=True)
    _evaluated(session, SKIP_SECTION, fired=True)

    assert _counts(session) == {SKIP_SECTION.id: (2, 1), SKIP_PROBES.id: (1, 1)}


def test_a_condition_never_evaluated_counts_zero(session):
    _evaluated(session, SKIP_SECTION, fired=True)

    assert _counts(session)[SKIP_PROBES.id] == (0, 0)


def test_only_the_cohort_s_evaluations_count(session):
    _evaluated(session, SKIP_SECTION, fired=True, interview_id=OUT_OF_COHORT)
    _evaluated(session, SKIP_SECTION, fired=True, interview_id=ELSEWHERE)

    assert _counts(session)[SKIP_SECTION.id] == (0, 0)


def test_an_evaluation_without_a_condition_id_is_not_counted(session):
    # As the migration leaves one it could not tie to a question.
    without_id = SKIP_SECTION.model_dump(mode="json", exclude={"id"})
    _evaluated(session, json.dumps(without_id), fired=True)
    _evaluated(session, "not json", fired=True)

    assert _counts(session) == {SKIP_SECTION.id: (0, 0), SKIP_PROBES.id: (0, 0)}
