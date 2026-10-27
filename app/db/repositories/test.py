import random
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta

from pydantic import UUID4
from sqlalchemy import Column, delete, exists, func, or_, select, update
from sqlalchemy.exc import NoResultFound

from ainterviewer.synthesize.interviewees import BackgroundInfoOptions
from ainterviewer.types import InterviewStatus
from ainterviewer.utils import now

from ...schemas.projects import (
    ExperimentCreate,
    ExperimentProjectPublic,
    ExperimentPublic,
    ExperimentUpdate,
)
from ...schemas.testing import (
    SynthesizeRequest,
    TestRunCreate,
    TestRunPublic,
    TestSetupCreate,
    TestSetupPublic,
)
from ...types import ExperimentAllocation, ProjectStatus, TestRunStatus
from ..tables import (
    ExperimentAssignmentTable,
    ExperimentProjectTable,
    ExperimentTable,
    IntervieweeTable,
    InterviewTable,
    MessageTable,
    ProjectContentTable,
    ProjectTable,
    TaskTable,
    TestRunTable,
    TestSetupTable,
)
from .base import BaseRepository
from .errors import ContentRevisionError


class TestRepository(BaseRepository):
    """Repository for TestSetup, TestRun, and Experiment operations."""

    # ==================== Test Setup Methods ====================

    def get_test_setups(self, project_id: UUID4) -> list[TestSetupPublic]:
        statement = (
            select(TestSetupTable)
            .where(TestSetupTable.project_id == project_id)
            .order_by(Column("created_at").desc())
        )
        tests = self.session.execute(statement).scalars().all()

        return [TestSetupPublic.model_validate(test) for test in tests]

    def create_test_setup(self, test_setup_create: TestSetupCreate) -> TestSetupPublic:
        test = TestSetupTable(**test_setup_create.model_dump())

        self.session.add(test)
        self.session.commit()
        self.session.refresh(test)

        return TestSetupPublic.model_validate(test)

    def test_in_project(self, test_id: UUID4, project_id: UUID4) -> bool:
        return bool(
            self.session.scalar(
                select(
                    exists().where(
                        TestSetupTable.id == test_id,
                        TestSetupTable.project_id == project_id,
                    )
                )
            )
        )

    def run_in_project(self, test_run_id: UUID4, project_id: UUID4) -> bool:
        return bool(
            self.session.scalar(
                select(
                    exists()
                    .where(TestRunTable.id == test_run_id)
                    .where(TestRunTable.test_setup_id == TestSetupTable.id)
                    .where(TestSetupTable.project_id == project_id)
                )
            )
        )

    def get_setup_interview_ids(self, test_id: UUID4) -> list[uuid.UUID]:
        """The interviews a test setup delete takes with it."""
        statement = (
            select(InterviewTable.id)
            .join(TestRunTable, InterviewTable.test_run_id == TestRunTable.id)
            .where(TestRunTable.test_setup_id == test_id)
        )
        return list(self.session.execute(statement).scalars())

    def delete_test_setup(self, test_id: UUID4):
        """Delete a test setup along with its runs and the interviews they
        produced.

        The children are deleted explicitly rather than left to ON DELETE
        CASCADE, which only fires while foreign keys are enforced. Enforcement
        is the default but can be switched off (see app/db/pragmas.py), and the
        rows would be orphaned instead.
        """
        run_ids = select(TestRunTable.id).where(TestRunTable.test_setup_id == test_id)
        interview_ids = select(InterviewTable.id).where(
            InterviewTable.test_run_id.in_(run_ids)
        )

        # Annotations and comments hang off the messages, so they go first of
        # all; see BaseRepository._delete_message_children.
        self._delete_message_children(
            select(MessageTable.id).where(MessageTable.interview_id.in_(interview_ids))
        )
        # Interview children next, so the subquery above still resolves.
        for table in (MessageTable, TaskTable, IntervieweeTable):
            self.session.execute(
                delete(table).where(table.interview_id.in_(interview_ids))
            )
        self.session.execute(
            delete(InterviewTable).where(InterviewTable.test_run_id.in_(run_ids))
        )
        self.session.execute(
            delete(TestRunTable).where(TestRunTable.test_setup_id == test_id)
        )
        self.session.execute(delete(TestSetupTable).where(TestSetupTable.id == test_id))
        self.session.commit()

    def get_test(self, test_id: UUID4) -> TestSetupPublic:
        statement = select(TestSetupTable).where(TestSetupTable.id == test_id)
        test = self.session.execute(statement).scalar_one()
        return TestSetupPublic.model_validate(test)

    def update_test_setup_settings(
        self, test_id: UUID4, request: SynthesizeRequest
    ) -> TestSetupPublic:
        statement = (
            update(TestSetupTable)
            .where(TestSetupTable.id == test_id)
            .values(
                answering_model=request.answering_model,
                n_interviews=request.n_interviews,
                language=request.language,
                delay_before_answers=request.delay_before_answers,
            )
            .returning(TestSetupTable)
        )
        test = self.session.execute(statement).scalar_one()
        self.session.commit()

        return TestSetupPublic.model_validate(test)

    def get_setup_revision(self, test_id: UUID4) -> int:
        """What `If-Match` must carry to save the setup page."""
        return self.session.execute(
            select(TestSetupTable.revision).where(TestSetupTable.id == test_id)
        ).scalar_one()

    def _write_setup(self, test_id: UUID4, revision: int, **values) -> int:
        """Save the setup page's `values` if the setup is still at `revision`,
        moving it on; ContentRevisionError if someone saved since. The new
        revision."""
        test = self.session.execute(
            select(TestSetupTable).where(TestSetupTable.id == test_id)
        ).scalar_one()
        if test.revision != revision:
            raise ContentRevisionError(revision, test.revision, "test setup")
        for key, value in values.items():
            setattr(test, key, value)
        test.revision += 1
        self.session.commit()
        return test.revision

    def get_background_info(
        self, project_id: UUID4, test_id: UUID4
    ) -> BackgroundInfoOptions:
        statement = select(TestSetupTable).where(
            TestSetupTable.project_id == project_id,
            TestSetupTable.id == test_id,
        )
        test = self.session.execute(statement).scalar_one()
        return test.background_info

    def update_background_info(
        self, test_id: UUID4, background_info: BackgroundInfoOptions, revision: int
    ) -> int:
        return self._write_setup(test_id, revision, background_info=background_info)

    def get_fixed_personas(self, project_id: UUID4, test_id: UUID4) -> list[str] | None:
        statement = select(TestSetupTable).where(
            TestSetupTable.project_id == project_id,
            TestSetupTable.id == test_id,
        )
        test = self.session.execute(statement).scalar_one()

        return test.fixed_personas

    def update_fixed_personas(
        self, test_id: UUID4, personas: list[str], revision: int
    ) -> int:
        return self._write_setup(test_id, revision, fixed_personas=personas)

    def get_fixed_answers(self, test_id: UUID4) -> dict[uuid.UUID, str] | None:
        statement = select(TestSetupTable).where(TestSetupTable.id == test_id)
        test = self.session.execute(statement).scalar_one()
        if test.fixed_answers is None:
            return None
        return {uuid.UUID(key): answer for key, answer in test.fixed_answers.items()}

    def update_fixed_answers(
        self, test_id: UUID4, answers: dict[UUID4, str], revision: int
    ) -> int:
        return self._write_setup(
            test_id,
            revision,
            fixed_answers={str(key): value for key, value in answers.items()},
        )

    # ==================== Test Run Methods ====================

    def create_test_run(self, test_run: TestRunCreate) -> UUID4:
        test_run_new = TestRunTable(**test_run.model_dump())
        self.session.add(test_run_new)
        self.session.commit()
        self.session.refresh(test_run_new)

        return test_run_new.id

    def get_test_status(self, test_setup_id: UUID4) -> Sequence[TestRunPublic]:
        statement = (
            select(TestRunTable)
            .where(TestRunTable.test_setup_id == test_setup_id)
            .order_by(Column("created_at").desc())
        )
        test_runs = self.session.execute(statement).scalars().all()

        return [TestRunPublic.model_validate(test_run) for test_run in test_runs]

    def update_test_run_status(
        self, test_setup_id: UUID4, test_run_id: UUID4, status: TestRunStatus
    ):
        statement = (
            update(TestRunTable)
            .where(
                TestRunTable.test_setup_id == test_setup_id,
                TestRunTable.id == test_run_id,
            )
            .values(status=status)
        )
        self.session.execute(statement)
        self.session.commit()

    def count_active_synthetic_interviews(
        self, user_id: UUID4, max_age: timedelta
    ) -> int:
        """Sum the interviews of the test runs a user started that are in flight.

        Runs older than `max_age` are left out: a backend restart mid-run
        leaves its status at RUNNING for good, and without the cutoff that run
        would count against the user forever.
        """
        statement = select(func.coalesce(func.sum(TestRunTable.n_interviews), 0)).where(
            TestRunTable.started_by_id == user_id,
            TestRunTable.status.in_([TestRunStatus.PENDING, TestRunStatus.RUNNING]),
            TestRunTable.created_at > now() - max_age,
        )
        return self.session.execute(statement).scalar_one()

    def count_started_synthetic_interviews(
        self, user_id: UUID4, since: datetime | None = None
    ) -> int:
        """Sum the interviews of every test run a user started, whatever its status.

        With `since`, only runs created after it count; without, all of them do.
        """
        statement = select(func.coalesce(func.sum(TestRunTable.n_interviews), 0)).where(
            TestRunTable.started_by_id == user_id
        )
        if since is not None:
            statement = statement.where(TestRunTable.created_at > since)
        return self.session.execute(statement).scalar_one()

    # ==================== Experiment Methods ====================

    def create_experiment(
        self, experiment: ExperimentCreate, user_id: UUID4
    ) -> ExperimentPublic:
        new_experiment = ExperimentTable(
            title=experiment.title,
            user_id=user_id,
            allocation=experiment.allocation,
        )
        self.session.add(new_experiment)
        self.session.flush()

        for project in experiment.projects:
            self.session.add(
                ExperimentProjectTable(
                    experiment_id=new_experiment.id,
                    project_id=project.project_id,
                    weight=project.weight,
                    target=project.target,
                )
            )
            if project.experiment_only is not None:
                self.session.execute(
                    update(ProjectTable)
                    .where(ProjectTable.id == project.project_id)
                    .values(experiment_only=project.experiment_only)
                )

        self.session.commit()
        self.session.refresh(new_experiment)

        return self._to_experiment_public(new_experiment)

    def update_experiment(
        self, experiment_id: UUID4, experiment: ExperimentUpdate, user_id: UUID4
    ) -> ExperimentPublic:
        """Replace the settings of an experiment owned by `user_id`.

        A kept project keeps its link, so its `added_at` and counts carry on.
        A removed project has its `experiment_only` cleared unless another
        experiment still uses it, as deleting the experiment does by default:
        left set, a project nothing can assign to refuses everyone. Removing
        a project respondents were already given is the caller's to refuse.

        Raises NoResultFound for an experiment the user does not own.
        """
        existing = self.session.execute(
            select(ExperimentTable).where(
                ExperimentTable.id == experiment_id,
                ExperimentTable.user_id == user_id,
            )
        ).scalar_one()
        existing.title = experiment.title
        existing.allocation = experiment.allocation

        arms = {arm.project_id: arm for arm in existing.experiment_projects}
        wanted = {project.project_id for project in experiment.projects}
        removed = arms.keys() - wanted
        for project_id in removed:
            existing.experiment_projects.remove(arms[project_id])
        if removed:
            # Flushed first, so the removed links no longer count as others.
            self.session.flush()
            others = select(ExperimentProjectTable.project_id)
            self.session.execute(
                update(ProjectTable)
                .where(ProjectTable.id.in_(removed), ProjectTable.id.not_in(others))
                .values(experiment_only=False)
            )

        for project in experiment.projects:
            arm = arms.get(project.project_id)
            if arm is None:
                arm = arms[project.project_id] = ExperimentProjectTable(
                    project_id=project.project_id
                )
                existing.experiment_projects.append(arm)
            arm.weight = project.weight
            arm.target = project.target
            if project.experiment_only is not None:
                self.session.execute(
                    update(ProjectTable)
                    .where(ProjectTable.id == project.project_id)
                    .values(experiment_only=project.experiment_only)
                )

        self.session.commit()
        self.session.refresh(existing)

        return self._to_experiment_public(existing)

    def get_experiments(self, user_id: UUID4) -> list[ExperimentPublic]:
        statement = (
            select(ExperimentTable)
            .where(ExperimentTable.user_id == user_id)
            .order_by(Column("created_at").desc())
        )
        experiments = self.session.execute(statement).scalars().all()

        return [self._to_experiment_public(experiment) for experiment in experiments]

    def get_experiment(self, experiment_id: UUID4, user_id: UUID4) -> ExperimentPublic:
        statement = select(ExperimentTable).where(
            ExperimentTable.id == experiment_id,
            ExperimentTable.user_id == user_id,
        )
        experiment = self.session.execute(statement).scalar_one()

        return self._to_experiment_public(experiment)

    def delete_experiment(
        self, experiment_id: UUID4, user_id: UUID4, clear_experiment_only: bool = False
    ) -> None:
        """Delete an experiment owned by `user_id`, unlinking its projects and
        interviews.

        Interviews are kept and their reference cleared, matching the SET NULL
        that `interview.experiment_id` declares; the project links and
        assignments are deleted. Both are done explicitly so they hold with
        foreign-key enforcement switched off too -- see `delete_test_setup`.

        `clear_experiment_only` lets the experiment's projects take respondents
        from their own links again, unless another experiment still uses them:
        left set, a project nothing can assign to refuses everyone.
        """
        owned = select(ExperimentTable.id).where(
            ExperimentTable.id == experiment_id,
            ExperimentTable.user_id == user_id,
        )

        if clear_experiment_only:
            others = select(ExperimentProjectTable.project_id).where(
                ExperimentProjectTable.experiment_id != experiment_id
            )
            self.session.execute(
                update(ProjectTable)
                .where(
                    ProjectTable.id.in_(
                        select(ExperimentProjectTable.project_id).where(
                            ExperimentProjectTable.experiment_id.in_(owned)
                        )
                    ),
                    ProjectTable.id.not_in(others),
                )
                .values(experiment_only=False)
            )
        self.session.execute(
            delete(ExperimentAssignmentTable).where(
                ExperimentAssignmentTable.experiment_id.in_(owned)
            )
        )
        self.session.execute(
            delete(ExperimentProjectTable).where(
                ExperimentProjectTable.experiment_id.in_(owned)
            )
        )
        self.session.execute(
            update(InterviewTable)
            .where(InterviewTable.experiment_id.in_(owned))
            .values(experiment_id=None)
        )
        self.session.execute(
            delete(ExperimentTable).where(
                ExperimentTable.id == experiment_id,
                ExperimentTable.user_id == user_id,
            )
        )
        self.session.commit()

    def _arm_counts(self, experiment_id: UUID4) -> dict[uuid.UUID, tuple[int, int]]:
        """Per project: (assignments, completed interviews) in the experiment."""
        assigned = dict(
            self.session.execute(
                select(ExperimentAssignmentTable.project_id, func.count())
                .where(ExperimentAssignmentTable.experiment_id == experiment_id)
                .group_by(ExperimentAssignmentTable.project_id)
            ).all()
        )
        completed = dict(
            self.session.execute(
                select(InterviewTable.project_id, func.count())
                .where(
                    InterviewTable.experiment_id == experiment_id,
                    InterviewTable.status == InterviewStatus.COMPLETED,
                )
                .group_by(InterviewTable.project_id)
            ).all()
        )
        return {
            project_id: (assigned.get(project_id, 0), completed.get(project_id, 0))
            for project_id in assigned.keys() | completed.keys()
        }

    def _to_experiment_public(self, experiment: ExperimentTable) -> ExperimentPublic:
        """Convert an ExperimentTable to ExperimentPublic with projects."""
        counts = self._arm_counts(experiment.id)
        projects = [
            ExperimentProjectPublic(
                id=ep.id,
                project_id=ep.project_id,
                weight=ep.weight,
                target=ep.target,
                added_at=ep.added_at,
                assigned=counts.get(ep.project_id, (0, 0))[0],
                completed=counts.get(ep.project_id, (0, 0))[1],
            )
            for ep in experiment.experiment_projects
        ]

        return ExperimentPublic(
            id=experiment.id,
            title=experiment.title,
            user_id=experiment.user_id,
            created_at=experiment.created_at,
            status=experiment.status,
            allocation=experiment.allocation,
            projects=projects,
        )

    # ==================== Experiment assignment ====================

    def _open_projects(self, experiment_id: UUID4) -> set[uuid.UUID]:
        """The experiment's projects a respondent can actually be sent to:
        active and published. An unpublished one would only show "not
        published yet"."""
        return set(
            self.session.execute(
                select(ExperimentProjectTable.project_id)
                .join(
                    ProjectTable, ProjectTable.id == ExperimentProjectTable.project_id
                )
                .join(
                    ProjectContentTable,
                    ProjectContentTable.project_id == ProjectTable.id,
                )
                .where(
                    ExperimentProjectTable.experiment_id == experiment_id,
                    ProjectTable.status == ProjectStatus.ACTIVE,
                    ProjectContentTable.published_version_id.is_not(None),
                )
            ).scalars()
        )

    def _draw(
        self, experiment: ExperimentTable, rng: random.Random
    ) -> uuid.UUID | None:
        """A project for a new respondent, or None when none can take one.

        A project takes new respondents while it is open, has a weight above
        zero (unset counts as 1) and has not reached its target of completed
        interviews.
        """
        open_projects = self._open_projects(experiment.id)
        counts = self._arm_counts(experiment.id)
        arms: dict[uuid.UUID, float] = {}
        for arm in experiment.experiment_projects:
            weight = 1.0 if arm.weight is None else arm.weight
            completed = counts.get(arm.project_id, (0, 0))[1]
            if (
                arm.project_id in open_projects
                and weight > 0
                and (arm.target is None or completed < arm.target)
            ):
                arms[arm.project_id] = weight
        if not arms:
            return None

        projects = list(arms)
        if experiment.allocation == ExperimentAllocation.RANDOM:
            candidates = projects
        else:
            # Furthest below its weighted share of the next respondent's
            # total; ties are drawn by weight like the random mode.
            index = 0 if experiment.allocation == ExperimentAllocation.BALANCED else 1
            done = {p: counts.get(p, (0, 0))[index] for p in projects}
            total, weights = sum(done.values()) + 1, sum(arms.values())
            deficit = {p: arms[p] / weights * total - done[p] for p in projects}
            most = max(deficit.values())
            candidates = [p for p in projects if most - deficit[p] < 1e-9]
        return rng.choices(candidates, weights=[arms[p] for p in candidates])[0]

    def enter_experiment(
        self,
        experiment_id: UUID4,
        respondent_hash: str,
        pid: str | None = None,
        rng: random.Random | None = None,
    ) -> ExperimentAssignmentTable | None:
        """The respondent's assignment in this experiment, drawn if they have
        none. None when the experiment is closed to them.

        A known participant is matched by pid, anyone else by the hash of their
        browser's `respondent` cookie. An existing assignment holds while its
        project is open, whether or not it still takes *new* respondents: a
        full target or a zero weight closes a project to newcomers only.

        Raises NoResultFound for an experiment that does not exist.
        """
        experiment = self.session.get(ExperimentTable, experiment_id)
        if experiment is None:
            raise NoResultFound(f"No experiment {experiment_id}")
        if experiment.status != ProjectStatus.ACTIVE:
            return None

        # A browser's own assignment is the one without a pid: the same browser
        # may also have entered as a known participant, whose assignment
        # records the browser it was last started in.
        key = (
            ExperimentAssignmentTable.pid == pid
            if pid is not None
            else (ExperimentAssignmentTable.respondent_hash == respondent_hash)
            & ExperimentAssignmentTable.pid.is_(None)
        )
        assignment = self.session.execute(
            select(ExperimentAssignmentTable)
            .where(ExperimentAssignmentTable.experiment_id == experiment_id, key)
            .order_by(ExperimentAssignmentTable.created_at)
            .limit(1)
        ).scalar_one_or_none()

        if assignment is None or assignment.project_id not in self._open_projects(
            experiment_id
        ):
            project_id = self._draw(experiment, rng or random.Random())
            if project_id is None:
                return None
            if assignment is None:
                assignment = ExperimentAssignmentTable(
                    experiment_id=experiment_id,
                    pid=pid,
                    respondent_hash=None if pid is not None else respondent_hash,
                )
                self.session.add(assignment)
            assignment.project_id = project_id
            assignment.interview_id = None

        assignment.entered_at = now()
        self.session.commit()
        self.session.refresh(assignment)
        return assignment

    def assignment_for_interview(
        self, project_id: UUID4, respondent_hash: str | None, pid: str | None
    ) -> ExperimentAssignmentTable | None:
        """The assignment a new interview in this project belongs to: the
        respondent's most recently entered one for it, by browser or by pid."""
        keys = []
        if respondent_hash is not None:
            keys.append(ExperimentAssignmentTable.respondent_hash == respondent_hash)
        if pid is not None:
            keys.append(ExperimentAssignmentTable.pid == pid)
        if not keys:
            return None
        return self.session.execute(
            select(ExperimentAssignmentTable)
            .where(ExperimentAssignmentTable.project_id == project_id, or_(*keys))
            .order_by(ExperimentAssignmentTable.entered_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    def record_assignment_interview(
        self, assignment_id: UUID4, interview_id: UUID4, respondent_hash: str | None
    ) -> None:
        """Note the interview an assignment led to, and the browser it was
        started in: entering again from that browser resumes it. A browser
        without a `respondent` cookie (a known participant's direct link)
        leaves the browser recorded before."""
        values: dict = {"interview_id": interview_id}
        if respondent_hash is not None:
            values["respondent_hash"] = respondent_hash
        self.session.execute(
            update(ExperimentAssignmentTable)
            .where(ExperimentAssignmentTable.id == assignment_id)
            .values(**values)
        )
        self.session.commit()
