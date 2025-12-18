from fastapi import APIRouter, BackgroundTasks, HTTPException, Response
from fastapi.responses import RedirectResponse
from pydantic import UUID4
from sqlalchemy.exc import NoResultFound
from uvicorn.config import logger

from ainterviewer.synthesize.interviewees import BackgroundInfoOptions
from ainterviewer.types import TestType

from ...db.models import (
    IntervieweeCreate,
    TestRunCreate,
    TestSetupCreate,
    TestSetupPublic,
)
from ...dependencies import DBSession, UserToken
from ...types import TestRunStatus
from .core import (
    run_synthesis_job_fixed_answers,
    run_synthesis_job_shuffled_ai,
)
from .models import (
    SynthesizeRequest,
    SynthesizeResponse,
    UpdateBackgroundInfoRequest,
    UpdateFixedAnswersRequest,
)

router = APIRouter(dependencies=[])


@router.get("/projects/{project_id}/tests/{test_id}/background_info")
async def get_background_info(
    response: Response,
    project_id: UUID4,
    test_id: UUID4,
    db: DBSession,
) -> BackgroundInfoOptions:
    try:
        return db.tests.get_background_info(project_id, test_id)
    except NoResultFound:
        response = RedirectResponse("/dashboard/projects/tests", status_code=302)
        response.delete_cookie("test_id")
        return response


@router.post("/projects/{project_id}/tests/{test_id}/background_info")
async def update_background_info(
    project_id: UUID4,
    test_id: UUID4,
    request: UpdateBackgroundInfoRequest,
    db: DBSession,
):
    return db.tests.update_background_info(test_id, request.background_info)


@router.get("/projects/{project_id}/tests/{test_id}/fixed_answers")
async def get_fixed_answers(
    project_id: UUID4,
    test_id: UUID4,
    db: DBSession,
):
    return db.tests.get_fixed_answers(test_id)


@router.post("/projects/{project_id}/tests/{test_id}/fixed_answers")
async def update_fixed_answers(
    project_id: UUID4,
    test_id: UUID4,
    request: UpdateFixedAnswersRequest,
    db: DBSession,
):
    return db.tests.update_fixed_answers(test_id, request.answers)


@router.get("/projects/{project_id}/tests")
async def get_test_setups(
    project_id: UUID4,
    db: DBSession,
    jwt: UserToken,
) -> list[TestSetupPublic]:
    return db.tests.get_test_setups(project_id)


@router.post("/projects/{project_id}/tests")
async def create_test_setup(
    test_setup: TestSetupCreate,
    db: DBSession,
    jwt: UserToken,
) -> TestSetupPublic:
    test_setup_public = db.tests.create_test_setup(test_setup)
    return test_setup_public


@router.post("/interviewee")
async def add_interviewee(
    interviewee: IntervieweeCreate,
    db: DBSession,
):
    db.interviews.add_interviewee(interviewee)


@router.get("/projects/{project_id}/tests/{test_id}/status")
async def get_test_status(
    project_id: UUID4,
    test_id: UUID4,
    db: DBSession,
    jwt: UserToken,
):
    return db.tests.get_test_status(test_id)


@router.post("/projects/{project_id}/tests/{test_id}/run")
async def run_synthetic_test(
    project_id: UUID4,
    test_id: UUID4,
    request: SynthesizeRequest,
    background_tasks: BackgroundTasks,
    db: DBSession,
    jwt: UserToken,
):
    # TODO:
    # Switch to Celery or similar framework for better control and management
    # of background tasks
    # https://fastapi.tiangolo.com/tutorial/background-tasks/#create-a-task-function
    # https://docs.celeryq.dev/en/stable/

    def handle_exceptions(func):
        async def wrapper(*args, **kwargs):
            try:
                results = await func(*args, **kwargs)

                for result in results:
                    if isinstance(result, Exception):
                        raise result

                db.tests.update_test_run_status(
                    test_setup_id=test_id,
                    test_run_id=test_run_id,
                    status=TestRunStatus.COMPLETED,
                )
            except Exception as e:
                logger.error("Error running synthesis job")
                db.tests.update_test_run_status(
                    test_setup_id=test_id,
                    test_run_id=test_run_id,
                    status=TestRunStatus.FAILED,
                )
                raise e

        return wrapper

    test_setup = db.tests.update_test_setup_settings(test_id, request)
    # test_setup = db.tests.get_test(test_id)

    test_run_id = db.tests.create_test_run(
        TestRunCreate(test_setup_id=test_id, **request.model_dump())
    )

    exception = None

    match test_setup.type:
        case TestType.SHUFFLED_AI:
            if not request.answering_model:
                exception = HTTPException(
                    status_code=400,
                    detail="Answering model is required for SHUFFLED_AI test type",
                )

            # Start the synthesis job in the background
            background_tasks.add_task(
                handle_exceptions(run_synthesis_job_shuffled_ai),
                project_id=str(project_id),
                background_info_options=test_setup.background_info,
                n_interviews=request.n_interviews,
                answering_model=request.answering_model,
                language=request.language,
                delay_before_answer=request.delay_before_answers,
            )
        case TestType.FIXED_ANSWERS:
            background_tasks.add_task(
                handle_exceptions(run_synthesis_job_fixed_answers),
                project_id=str(project_id),
                fixed_answers=test_setup.fixed_answers,
                n_interviews=request.n_interviews,
                language=request.language,
                delay_before_answer=request.delay_before_answers,
            )

        case TestType.FIXED_AI:
            exception = HTTPException(
                status_code=400,
                detail="Fixed AI test type is not yet implemented",
            )

    if exception:
        return exception

    db.tests.update_test_run_status(
        test_setup_id=test_id,
        test_run_id=test_run_id,
        status=TestRunStatus.RUNNING,
    )

    return SynthesizeResponse(
        project_id=project_id,
        message=f"Synthesis job started with {request.n_interviews} interviews",
        status="initializing",
    )
