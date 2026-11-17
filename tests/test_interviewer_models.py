"""A simulation can run its interviews on other models than the project's, to
compare them: one run per interviewer model, the same synthetic interviewees
in each, and the runs read back side by side.

The project's agent configs are untouched; the run records the model and the
interview websocket applies it (`with_interviewer_model`).
"""

import asyncio
import uuid

import pytest
from fastapi import BackgroundTasks, HTTPException, Request
from sqlalchemy import create_engine
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from ainterviewer.agents.config import AgentConfigs
from ainterviewer.settings import settings as lib_settings
from ainterviewer.types import InterviewStatus, MessageRole, TestType
from app.api.dashboard.synthesize import run_synthetic_test
from app.api.websockets.interviews.ai import with_interviewer_model
from app.auth import AuthToken
from app.db.crud import InterviewDataBase
from app.db.tables import (
    Base,
    IntervieweeTable,
    InterviewTable,
    MessageTable,
    TestRunTable,
    TestSetupTable,
)
from app.schemas.testing import SynthesizeRequest
from app.types import InterviewType, Scope
from tests.conftest import TEST_ONLY_MODEL

PROJECT = uuid.uuid4()
LOCAL = "openai/gpt-oss-120b"


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _setup(session, type: TestType, **values) -> TestSetupTable:
    setup = TestSetupTable(
        id=uuid.uuid4(),
        project_id=PROJECT,
        type=type,
        answering_model=LOCAL,
        **values,
    )
    session.add(setup)
    session.commit()
    return setup


def _start(session, setup, models, n=3, scope=Scope.USER):
    request = Request({"type": "http", "headers": [(b"cookie", b"access_token=t")]})
    tasks = BackgroundTasks()
    response = asyncio.run(
        run_synthetic_test(
            request=request,
            project_id=PROJECT,
            test_id=setup.id,
            request_data=SynthesizeRequest(
                n_interviews=n, answering_model=LOCAL, interviewer_models=models
            ),
            background_tasks=tasks,
            db=InterviewDataBase(session),
            jwt=AuthToken(user_id=uuid.uuid4(), scope=scope),
        )
    )
    return response, tasks


# ==================== Starting runs ====================


def test_one_run_per_interviewer_model(session):
    setup = _setup(
        session, TestType.FIXED_ANSWERS, fixed_answers={str(uuid.uuid4()): "Yes."}
    )

    response, tasks = _start(session, setup, [LOCAL, TEST_ONLY_MODEL])

    runs = {run.id: run.interviewer_model for run in session.query(TestRunTable).all()}
    # In the order the models were asked for.
    assert [runs[i] for i in response.test_run_ids] == [LOCAL, TEST_ONLY_MODEL]
    assert len(tasks.tasks) == 2


def test_no_interviewer_model_is_one_run_on_the_projects_models(session):
    setup = _setup(
        session, TestType.FIXED_ANSWERS, fixed_answers={str(uuid.uuid4()): "Yes."}
    )

    response, _ = _start(session, setup, [])

    (run,) = session.query(TestRunTable).all()
    assert run.interviewer_model is None
    assert response.test_run_ids == [run.id]


def test_every_run_interviews_the_same_people(session):
    setup = _setup(
        session, TestType.FIXED_AI, fixed_personas=["a nurse", "a farmer", "a pilot"]
    )

    _, tasks = _start(session, setup, [LOCAL, TEST_ONLY_MODEL], n=6)

    first, second = (task.kwargs["personas"] for task in tasks.tasks)
    assert len(first) == 6
    assert first == second


def test_an_unknown_model_is_refused_before_anything_starts(session):
    setup = _setup(
        session, TestType.FIXED_ANSWERS, fixed_answers={str(uuid.uuid4()): "Yes."}
    )

    with pytest.raises(HTTPException) as exc:
        _start(session, setup, [LOCAL, "not-a-model"])

    assert exc.value.status_code == 400
    assert session.query(TestRunTable).count() == 0


def test_demo_accounts_choose_from_the_demo_models(session):
    setup = _setup(
        session, TestType.FIXED_ANSWERS, fixed_answers={str(uuid.uuid4()): "Yes."}
    )
    assert TEST_ONLY_MODEL not in (lib_settings.llm.demo_models or [])

    with pytest.raises(HTTPException) as exc:
        _start(session, setup, [TEST_ONLY_MODEL], n=1, scope=Scope.DEMO)
    assert exc.value.status_code == 400


def test_duplicate_models_are_one_run(session):
    assert SynthesizeRequest(
        n_interviews=1, interviewer_models=[LOCAL, LOCAL]
    ).interviewer_models == [LOCAL]


# ==================== Running on the model ====================


def test_the_interview_agents_move_and_the_interviewee_stays():
    configs = AgentConfigs()
    configs.probing.temperature = 0.2

    moved = with_interviewer_model(configs, TEST_ONLY_MODEL)

    assert moved.probing.model == moved.security.model == TEST_ONLY_MODEL
    assert moved.history.model == moved.reformulation.model == TEST_ONLY_MODEL
    assert moved.answering.model == configs.answering.model
    assert moved.visual.model == configs.visual.model
    assert moved.probing.temperature == 0.2
    # A copy: the project's configs are not touched.
    assert configs.probing.model != TEST_ONLY_MODEL


def test_the_interview_reads_its_runs_model(session):
    setup = _setup(session, TestType.FIXED_ANSWERS)
    run = TestRunTable(test_setup_id=setup.id, n_interviews=1, interviewer_model=LOCAL)
    plain = TestRunTable(test_setup_id=setup.id, n_interviews=1)
    session.add_all([run, plain])
    session.flush()
    interviews = [
        InterviewTable(
            project_id=PROJECT,
            type=InterviewType.SYNTHETIC_TEST,
            test_run_id=r.id,
        )
        for r in (run, plain)
    ]
    respondent = InterviewTable(project_id=PROJECT, type=InterviewType.DISTRIBUTED)
    session.add_all([*interviews, respondent])
    session.commit()
    tests = InterviewDataBase(session).tests

    assert tests.interviewer_model(interviews[0].id) == LOCAL
    assert tests.interviewer_model(interviews[1].id) is None
    assert tests.interviewer_model(respondent.id) is None


# ==================== Comparing ====================


def _interview(session, run, persona, answers):
    interview = InterviewTable(
        project_id=PROJECT,
        type=InterviewType.SYNTHETIC_TEST,
        test_run_id=run.id,
        status=InterviewStatus.COMPLETED,
    )
    session.add(interview)
    session.flush()
    session.add(
        IntervieweeTable(
            interview_id=interview.id, project_id=PROJECT, interview_subject=persona
        )
    )
    for i, (role, content) in enumerate(answers):
        session.add(
            MessageTable(
                message_id=i,
                interview_id=interview.id,
                project_id=PROJECT,
                role=role,
                content=content,
                main_question=0,
            )
        )
    return interview


def test_runs_come_back_with_transcripts_and_personas(session):
    setup = _setup(session, TestType.FIXED_AI)
    runs = [
        TestRunTable(test_setup_id=setup.id, n_interviews=1, interviewer_model=m)
        for m in (LOCAL, TEST_ONLY_MODEL)
    ]
    session.add_all(runs)
    session.flush()
    for run in runs:
        _interview(
            session,
            run,
            "a nurse",
            [
                (MessageRole.SYSTEM, "instructions"),
                (MessageRole.ASSISTANT, f"Question from {run.interviewer_model}"),
                (MessageRole.USER, "An answer"),
                (MessageRole.ASSISTANT, "<|end_of_interview|>"),
            ],
        )
    session.commit()

    comparison = InterviewDataBase(session).tests.compare_runs(
        setup.id, [runs[1].id, runs[0].id]
    )

    assert [r.run.interviewer_model for r in comparison.runs] == [
        TEST_ONLY_MODEL,
        LOCAL,
    ]
    (interview,) = comparison.runs[0].interviews
    assert interview.persona == "a nurse"
    assert [(t.role, t.content) for t in interview.turns] == [
        (MessageRole.ASSISTANT, f"Question from {TEST_ONLY_MODEL}"),
        (MessageRole.USER, "An answer"),
    ]


def test_a_run_of_another_test_is_not_found(session):
    setup = _setup(session, TestType.FIXED_AI)
    other = _setup(session, TestType.FIXED_AI)
    run = TestRunTable(test_setup_id=other.id, n_interviews=1)
    session.add(run)
    session.commit()

    with pytest.raises(NoResultFound):
        InterviewDataBase(session).tests.compare_runs(setup.id, [run.id])
