from datetime import timedelta
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
)
from pydantic import UUID4
from sqlalchemy.exc import NoResultFound
from uvicorn.config import logger

from ainterviewer.settings import settings as lib_settings
from ainterviewer.synthesize.interviewees import (
    BackgroundInfoOptions,
    generate_synthetic_persons,
)
from ainterviewer.types import TestType
from ainterviewer.utils import now

from ...dependencies import DBSession, IfMatch, ProjectEditor, ProjectViewer, set_etag
from ...schemas.interviews import IntervieweeCreate, IntervieweePublic
from ...schemas.testing import (
    RunComparisonResponse,
    SynthesizeRequest,
    SynthesizeResponse,
    TestRunCreate,
    TestRunPublic,
    TestSetupCreate,
    TestSetupPublic,
    UpdateBackgroundInfoRequest,
    UpdateFixedAnswersRequest,
    UpdateFixedPersonasRequest,
)
from ...schemas.users import DemoBudget, DemoBudgetItem
from ...services import interview_storage
from ...synthesize.core import (
    draw_fixed_personas,
    run_synthesis_job_fixed_ai,
    run_synthesis_job_fixed_answers,
    run_synthesis_job_shuffled_ai,
)
from ...types import Scope, TestRunStatus
from ..errors import STALE_REVISION
from ..websockets.manager import session_manager

router = APIRouter(tags=["synthesize"])


def _require_test_in_project(project_id: UUID4, test_id: UUID4, db: DBSession) -> None:
    """404 unless `test_id` is a test setup of `project_id`.

    The role check on these routes is against the project in the path, and the
    repository methods act on `test_id` alone -- so without this an editor of
    one project could read, change, run or delete another project's test setup
    by naming its id. A 404 rather than a 403 so the answer does not confirm
    the id exists elsewhere.
    """
    if not db.tests.test_in_project(test_id, project_id):
        raise HTTPException(status_code=404, detail="Test setup not found")


#: For every route with `{test_id}` in its path.
_test_in_project = [Depends(_require_test_in_project)]

DEMO_DAILY_WINDOW = timedelta(days=1)


def get_demo_budget(db: DBSession, user_id: UUID4) -> DemoBudget:
    """The user's demo budgets, with their overrides applied, and their usage."""
    limits = db.users.get_demo_limits(user_id)
    return DemoBudget(
        concurrent=DemoBudgetItem(
            limit=limits.max_concurrent_synthetic_interviews,
            used=db.tests.count_active_synthetic_interviews(
                user_id, max_age=limits.active_run_max_age.to_timedelta()
            ),
        ),
        daily=DemoBudgetItem(
            limit=limits.daily_synthetic_interviews,
            used=db.tests.count_started_synthetic_interviews(
                user_id, since=now() - DEMO_DAILY_WINDOW
            ),
        ),
        lifetime=DemoBudgetItem(
            limit=limits.lifetime_synthetic_interviews,
            used=db.tests.count_started_synthetic_interviews(user_id),
        ),
    )


def check_demo_synthetic_limits(db: DBSession, user_id: UUID4, n_interviews: int):
    """Refuse a demo user's run with a 429 if it would exceed any of their budgets."""
    budget = get_demo_budget(db, user_id)

    lifetime = budget.lifetime
    if lifetime.used + n_interviews > lifetime.limit:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Your demo account can run at most {lifetime.limit} synthetic "
                f"interviews in total, and {lifetime.used} have already been "
                "started."
            ),
        )

    daily = budget.daily
    if daily.used + n_interviews > daily.limit:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Your demo account can run at most {daily.limit} synthetic "
                f"interviews per 24 hours, and {daily.used} have been started in "
                "the last 24 hours."
            ),
        )

    concurrent = budget.concurrent
    if concurrent.used + n_interviews > concurrent.limit:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Your demo account can run at most {concurrent.limit} synthetic "
                f"interviews at a time, and {concurrent.used} are already "
                "running. Wait for them to finish or run fewer interviews."
            ),
        )


@router.get(
    "/projects/{project_id}/tests/{test_id}/background_info",
    dependencies=_test_in_project,
)
async def get_background_info(
    response: Response,
    project_id: UUID4,
    test_id: UUID4,
    db: DBSession,
    jwt: ProjectViewer,
) -> BackgroundInfoOptions:
    set_etag(response, db.tests.get_setup_revision(test_id))
    return db.tests.get_background_info(project_id, test_id)


@router.post(
    "/projects/{project_id}/tests/{test_id}/background_info",
    dependencies=_test_in_project,
    responses=STALE_REVISION,
)
async def update_background_info(
    project_id: UUID4,
    test_id: UUID4,
    request: UpdateBackgroundInfoRequest,
    db: DBSession,
    jwt: ProjectEditor,
    revision: IfMatch,
    response: Response,
) -> None:
    set_etag(
        response,
        db.tests.update_background_info(test_id, request.background_info, revision),
    )


@router.get(
    "/projects/{project_id}/tests/{test_id}/fixed_personas",
    dependencies=_test_in_project,
)
async def get_fixed_personas(
    response: Response,
    project_id: UUID4,
    test_id: UUID4,
    db: DBSession,
    jwt: ProjectViewer,
) -> list[str] | None:
    set_etag(response, db.tests.get_setup_revision(test_id))
    return db.tests.get_fixed_personas(project_id, test_id)


@router.post(
    "/projects/{project_id}/tests/{test_id}/fixed_personas",
    dependencies=_test_in_project,
    responses=STALE_REVISION,
)
async def update_fixed_personas(
    project_id: UUID4,
    test_id: UUID4,
    request: UpdateFixedPersonasRequest,
    db: DBSession,
    jwt: ProjectEditor,
    revision: IfMatch,
    response: Response,
) -> None:
    set_etag(
        response,
        db.tests.update_fixed_personas(test_id, request.fixed_personas, revision),
    )


@router.get(
    "/projects/{project_id}/tests/{test_id}/fixed_answers",
    dependencies=_test_in_project,
)
async def get_fixed_answers(
    response: Response,
    project_id: UUID4,
    test_id: UUID4,
    db: DBSession,
    jwt: ProjectViewer,
) -> dict[UUID4, str] | None:
    set_etag(response, db.tests.get_setup_revision(test_id))
    return db.tests.get_fixed_answers(test_id)


@router.post(
    "/projects/{project_id}/tests/{test_id}/fixed_answers",
    dependencies=_test_in_project,
    responses=STALE_REVISION,
)
async def update_fixed_answers(
    project_id: UUID4,
    test_id: UUID4,
    request: UpdateFixedAnswersRequest,
    db: DBSession,
    jwt: ProjectEditor,
    revision: IfMatch,
    response: Response,
) -> None:
    set_etag(
        response, db.tests.update_fixed_answers(test_id, request.answers, revision)
    )


@router.get("/projects/{project_id}/tests")
async def get_test_setups(
    project_id: UUID4,
    db: DBSession,
    jwt: ProjectViewer,
) -> list[TestSetupPublic]:
    return db.tests.get_test_setups(project_id)


@router.post("/projects/{project_id}/tests")
async def create_test_setup(
    project_id: UUID4,
    test_setup: TestSetupCreate,
    db: DBSession,
    jwt: ProjectEditor,
) -> TestSetupPublic:
    # The role check is against the project in the path, so that is the
    # project the setup goes in -- never one named in the body.
    test_setup = test_setup.model_copy(update={"project_id": project_id})
    test_setup_public = db.tests.create_test_setup(test_setup)
    return test_setup_public


@router.delete("/projects/{project_id}/tests/{test_id}", dependencies=_test_in_project)
async def delete_test_setup(
    project_id: UUID4,
    test_id: UUID4,
    db: DBSession,
    jwt: ProjectEditor,
):
    interview_ids = db.tests.get_setup_interview_ids(test_id)
    await session_manager.end([project_id], interview_ids)
    db.tests.delete_test_setup(test_id)
    interview_storage.delete_interview_storage(interview_ids)


@router.post("/projects/{project_id}/interviewee")
async def add_interviewee(
    project_id: UUID4,
    interviewee: IntervieweeCreate,
    db: DBSession,
    jwt: ProjectEditor,
):
    # A synthetic agent posts this after creating its interview; if the test
    # setup was deleted in between, the interview is already gone.
    if not db.interviews.interview_in_project(interviewee.interview_id, project_id):
        raise HTTPException(status_code=404, detail="Interview not found")
    db.interviews.add_interviewee(project_id, interviewee)


@router.get("/projects/{project_id}/interviewee")
async def get_interviewee(
    project_id: UUID4,
    interview_id: UUID4,
    db: DBSession,
    jwt: ProjectViewer,
) -> IntervieweePublic:
    try:
        return db.interviews.get_interviewee(project_id, interview_id)
    except NoResultFound:
        raise HTTPException(status_code=404, detail="Interviewee not found")


@router.get(
    "/projects/{project_id}/tests/{test_id}/status", dependencies=_test_in_project
)
async def get_test_status(
    project_id: UUID4,
    test_id: UUID4,
    db: DBSession,
    jwt: ProjectViewer,
) -> list[TestRunPublic]:
    return list(db.tests.get_test_status(test_id))


@router.post(
    "/projects/{project_id}/tests/{test_id}/run", dependencies=_test_in_project
)
async def run_synthetic_test(
    request: Request,
    project_id: UUID4,
    test_id: UUID4,
    request_data: SynthesizeRequest,
    background_tasks: BackgroundTasks,
    db: DBSession,
    jwt: ProjectEditor,
) -> SynthesizeResponse:
    # TODO:
    # Switch to Celery or similar framework for better control and management
    # of background tasks
    # https://fastapi.tiangolo.com/tutorial/background-tasks/#create-a-task-function
    # https://docs.celeryq.dev/en/stable/

    def handle_exceptions(func, run_id: UUID4):
        # The run's id is passed rather than read from the loop below, which
        # would leave every run's status to the last one's.
        async def wrapper(*args, **kwargs):
            try:
                results = await func(*args, **kwargs)

                for result in results:
                    if isinstance(result, Exception):
                        raise result

                db.tests.update_test_run_status(
                    test_setup_id=test_id,
                    test_run_id=run_id,
                    status=TestRunStatus.COMPLETED,
                )
            except Exception:
                logger.error("Error running synthesis job")
                db.tests.update_test_run_status(
                    test_setup_id=test_id,
                    test_run_id=run_id,
                    status=TestRunStatus.FAILED,
                )
                raise

        return wrapper

    # Any model a project may configure, test-only ones included: a run is a
    # test, and trying a model before switching to it is the point.
    llm = lib_settings.llm
    offered = (
        (llm.demo_models or []) if jwt.scope == Scope.DEMO else llm.available_models
    )
    if unknown := [m for m in request_data.interviewer_models if m not in offered]:
        raise HTTPException(
            status_code=400, detail=f"Not an available model: {', '.join(unknown)}"
        )
    # One run per interviewer model, or one on the project's own models.
    interviewer_models: list[str | None] = [*request_data.interviewer_models] or [None]
    n_total = request_data.n_interviews * len(interviewer_models)

    if jwt.scope == Scope.DEMO:
        check_demo_synthetic_limits(db, jwt.user_id, n_total)

    test_setup = db.tests.update_test_setup_settings(test_id, request_data)

    # Checked before the run is created: a refusal after it would leave a run
    # that never starts.
    if test_setup.type == TestType.SHUFFLED_AI and not request_data.answering_model:
        raise HTTPException(
            status_code=400,
            detail="Answering model is required for SHUFFLED_AI test type",
        )
    if test_setup.type == TestType.FIXED_AI and not test_setup.fixed_personas:
        raise HTTPException(status_code=400, detail="Add a persona to run this test")

    # Drawn once, so every run being compared interviews the same people.
    personas = (
        draw_fixed_personas(test_setup.fixed_personas, request_data.n_interviews)
        if test_setup.type == TestType.FIXED_AI and test_setup.fixed_personas
        else None
    )
    subjects = (
        generate_synthetic_persons(
            test_setup.background_info, request_data.n_interviews
        )
        if test_setup.type == TestType.SHUFFLED_AI and test_setup.background_info
        else None
    )

    test_run_ids: list[UUID4] = []
    for interviewer_model in interviewer_models:
        test_run_id = db.tests.create_test_run(
            TestRunCreate(
                test_setup_id=test_id,
                started_by_id=jwt.user_id,
                language=request_data.language,
                n_interviews=request_data.n_interviews,
                answering_model=request_data.answering_model,
                interviewer_model=interviewer_model,
                delay_before_answers=request_data.delay_before_answers,
            )
        )
        test_run_ids.append(test_run_id)

        match test_setup.type:
            case TestType.FIXED_ANSWERS:
                background_tasks.add_task(
                    handle_exceptions(run_synthesis_job_fixed_answers, test_run_id),
                    project_id=str(project_id),
                    test_run_id=str(test_run_id),
                    user_token=request.cookies["access_token"],
                    fixed_answers=test_setup.fixed_answers or {},
                    n_interviews=request_data.n_interviews,
                    language=request_data.language,
                    delay_before_answer=request_data.delay_before_answers,
                )

            case TestType.FIXED_AI:
                background_tasks.add_task(
                    handle_exceptions(run_synthesis_job_fixed_ai, test_run_id),
                    project_id=str(project_id),
                    test_run_id=str(test_run_id),
                    user_token=request.cookies["access_token"],
                    fixed_personas=test_setup.fixed_personas,
                    n_interviews=request_data.n_interviews,
                    answering_model=request_data.answering_model,
                    language=request_data.language,
                    delay_before_answer=request_data.delay_before_answers,
                    personas=personas,
                )

            case TestType.SHUFFLED_AI:
                background_tasks.add_task(
                    handle_exceptions(run_synthesis_job_shuffled_ai, test_run_id),
                    project_id=str(project_id),
                    test_run_id=str(test_run_id),
                    user_token=request.cookies["access_token"],
                    background_info_options=test_setup.background_info,
                    n_interviews=request_data.n_interviews,
                    answering_model=request_data.answering_model,
                    language=request_data.language,
                    delay_before_answer=request_data.delay_before_answers,
                    subjects=subjects,
                )

        db.tests.update_test_run_status(
            test_setup_id=test_id,
            test_run_id=test_run_id,
            status=TestRunStatus.RUNNING,
        )

    runs = f" in each of {len(test_run_ids)} runs" if len(test_run_ids) > 1 else ""
    return SynthesizeResponse(
        project_id=project_id,
        message=f"Synthesis job started with {request_data.n_interviews} interviews{runs}",
        status="initializing",
        test_run_ids=test_run_ids,
    )


@router.get(
    "/projects/{project_id}/tests/{test_id}/runs/compare",
    dependencies=_test_in_project,
)
async def compare_test_runs(
    project_id: UUID4,
    test_id: UUID4,
    db: DBSession,
    jwt: ProjectViewer,
    run_id: Annotated[list[UUID4], Query(min_length=1, max_length=5)],
) -> RunComparisonResponse:
    """The runs' synthetic interviews with their transcripts, to read side by
    side: typically the runs one request started on several interviewer
    models, which interviewed the same people."""
    try:
        return db.tests.compare_runs(test_id, run_id)
    except NoResultFound as exc:
        raise HTTPException(status_code=404, detail="Test run not found") from exc
