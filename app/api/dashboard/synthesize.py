from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from pydantic import UUID4
from uvicorn.config import logger

from ainterviewer.synthesize.interviewees import BackgroundInfoOptions
from ainterviewer.types import TestType

from ...db.models import (
    IntervieweeCreate,
    TestRunCreate,
    TestSetupCreate,
    TestSetupPublic,
)
from ...dependencies import DBSession, ProjectEditor, ProjectViewer, UserToken
from ...synthesize.core import (
    run_synthesis_job_fixed_answers,
    run_synthesis_job_shuffled_ai,
    run_synthesis_job_fixed_ai,
)
from ...types import TestRunStatus
from ..request_models import (
    SynthesizeRequest,
    UpdateBackgroundInfoRequest,
    UpdateFixedAnswersRequest,
    UpdateFixedPersonasRequest,
)
from ..response_models import SynthesizeResponse

router = APIRouter(tags=["synthesize"])


@router.get("/projects/{project_id}/tests/{test_id}/background_info")
async def get_background_info(
    response: Response,
    project_id: UUID4,
    test_id: UUID4,
    db: DBSession,
    jwt: ProjectViewer,
) -> BackgroundInfoOptions:
    return db.tests.get_background_info(project_id, test_id)


@router.post("/projects/{project_id}/tests/{test_id}/background_info")
async def update_background_info(
    project_id: UUID4,
    test_id: UUID4,
    request: UpdateBackgroundInfoRequest,
    db: DBSession,
    jwt: ProjectEditor,
):
    return db.tests.update_background_info(test_id, request.background_info)


@router.get("/projects/{project_id}/tests/{test_id}/fixed_personas")
async def get_fixed_personas(
    response: Response,
    project_id: UUID4,
    test_id: UUID4,
    db: DBSession,
    jwt: ProjectViewer,
) -> list[str]:
    return db.tests.get_fixed_personas(project_id, test_id)


@router.post("/projects/{project_id}/tests/{test_id}/fixed_personas")
async def update_fixed_personas(
    project_id: UUID4,
    test_id: UUID4,
    request: UpdateFixedPersonasRequest,
    db: DBSession,
    jwt: ProjectEditor,
):
    return db.tests.update_fixed_personas(test_id, request.fixed_personas)


@router.get("/projects/{project_id}/tests/{test_id}/fixed_answers")
async def get_fixed_answers(
    project_id: UUID4,
    test_id: UUID4,
    db: DBSession,
    jwt: ProjectViewer,
):
    return db.tests.get_fixed_answers(test_id)


@router.post("/projects/{project_id}/tests/{test_id}/fixed_answers")
async def update_fixed_answers(
    project_id: UUID4,
    test_id: UUID4,
    request: UpdateFixedAnswersRequest,
    db: DBSession,
    jwt: ProjectEditor,
):
    return db.tests.update_fixed_answers(test_id, request.answers)


@router.get("/projects/{project_id}/tests")
async def get_test_setups(
    project_id: UUID4,
    db: DBSession,
    jwt: ProjectViewer,
) -> list[TestSetupPublic]:
    return db.tests.get_test_setups(project_id)


@router.post("/projects/{project_id}/tests")
async def create_test_setup(
    test_setup: TestSetupCreate,
    db: DBSession,
    jwt: ProjectEditor,
) -> TestSetupPublic:
    test_setup_public = db.tests.create_test_setup(test_setup)
    return test_setup_public


@router.delete("/projects/{project_id}/tests/{test_id}")
async def delete_test_setup(
    project_id: UUID4,
    test_id: UUID4,
    db: DBSession,
    jwt: ProjectEditor,
):
    db.tests.delete_test_setup(test_id)


@router.post("/projects/{project_id}/interviewee")
async def add_interviewee(
    project_id: UUID4,
    interviewee: IntervieweeCreate,
    db: DBSession,
    jwt: ProjectEditor,
):
    db.interviews.add_interviewee(project_id, interviewee)


@router.post("/projects/{project_id}/interviewee")
async def get_interviewee(
    project_id: UUID4,
    interview_id: UUID4,
    db: DBSession,
    jwt: UserToken,
):
    return db.interviews.get_interviewee(project_id, interview_id)


@router.get("/projects/{project_id}/tests/{test_id}/status")
async def get_test_status(
    project_id: UUID4,
    test_id: UUID4,
    db: DBSession,
    jwt: ProjectViewer,
):
    return db.tests.get_test_status(test_id)


@router.post("/projects/{project_id}/tests/{test_id}/run")
async def run_synthetic_test(
    request: Request,
    project_id: UUID4,
    test_id: UUID4,
    request_data: SynthesizeRequest,
    background_tasks: BackgroundTasks,
    db: DBSession,
    jwt: ProjectEditor,
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

    test_setup = db.tests.update_test_setup_settings(test_id, request_data)

    test_run_id = db.tests.create_test_run(
        TestRunCreate(test_setup_id=test_id, **request_data.model_dump())
    )

    exception = None

    match test_setup.type:
        case TestType.FIXED_ANSWERS:
            background_tasks.add_task(
                handle_exceptions(run_synthesis_job_fixed_answers),
                project_id=str(project_id),
                test_run_id=str(test_run_id),
                user_token=request.cookies["access_token"],
                fixed_answers=test_setup.fixed_answers,
                n_interviews=request_data.n_interviews,
                language=request_data.language,
                delay_before_answer=request_data.delay_before_answers,
            )

        case TestType.FIXED_AI:
            background_tasks.add_task(
                handle_exceptions(run_synthesis_job_fixed_ai),
                project_id=str(project_id),
                test_run_id=str(test_run_id),
                user_token=request.cookies["access_token"],
                fixed_personas=test_setup.fixed_personas,
                n_interviews=request_data.n_interviews,
                answering_model=request_data.answering_model,
                language=request_data.language,
                delay_before_answer=request_data.delay_before_answers,
            )

        case TestType.SHUFFLED_AI:
            if not request_data.answering_model:
                exception = HTTPException(
                    status_code=400,
                    detail="Answering model is required for SHUFFLED_AI test type",
                )

            # Start the synthesis job in the background
            background_tasks.add_task(
                handle_exceptions(run_synthesis_job_shuffled_ai),
                project_id=str(project_id),
                test_run_id=str(test_run_id),
                user_token=request.cookies["access_token"],
                background_info_options=test_setup.background_info,
                n_interviews=request_data.n_interviews,
                answering_model=request_data.answering_model,
                language=request_data.language,
                delay_before_answer=request_data.delay_before_answers,
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
        message=f"Synthesis job started with {request_data.n_interviews} interviews",
        status="initializing",
    )
