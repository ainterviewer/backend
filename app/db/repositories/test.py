from collections.abc import Sequence

from pydantic import UUID4
from sqlalchemy import Column, delete, select, update

from ainterviewer.synthesize.interviewees import BackgroundInfoOptions

from ...api.request_models import SynthesizeRequest
from ...types import TestRunStatus
from ..models import (
    ExperimentCreate,
    ExperimentProjectPublic,
    ExperimentPublic,
    TestRunCreate,
    TestRunPublic,
    TestSetupCreate,
    TestSetupPublic,
)
from ..tables import (
    ExperimentProjectTable,
    ExperimentTable,
    IntervieweeTable,
    InterviewTable,
    MessageTable,
    TaskTable,
    TestRunTable,
    TestSetupTable,
)
from .base import BaseRepository


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

    def delete_test_setup(self, test_id: UUID4):
        """Delete a test setup along with its runs and the interviews they
        produced.

        The children are deleted explicitly rather than left to ON DELETE
        CASCADE: SQLite only enforces foreign keys on connections that ran
        `PRAGMA foreign_keys=ON`, which the app does not currently do, so the
        cascade would not fire and the rows would be orphaned instead.
        """
        run_ids = select(TestRunTable.id).where(TestRunTable.test_setup_id == test_id)
        interview_ids = select(InterviewTable.id).where(
            InterviewTable.test_run_id.in_(run_ids)
        )

        # Interview children first, so the subquery above still resolves.
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
        self, test_id: UUID4, background_info: BackgroundInfoOptions
    ):
        statement = (
            update(TestSetupTable)
            .where(TestSetupTable.id == test_id)
            .values(background_info=background_info.model_dump())
        )
        self.session.execute(statement)
        self.session.commit()

    def get_fixed_personas(self, project_id: UUID4, test_id: UUID4) -> list[str] | None:
        statement = select(TestSetupTable).where(
            TestSetupTable.project_id == project_id,
            TestSetupTable.id == test_id,
        )
        test = self.session.execute(statement).scalar_one()

        return test.fixed_personas

    def update_fixed_personas(self, test_id: UUID4, personas: list[str]):
        statement = (
            update(TestSetupTable)
            .where(TestSetupTable.id == test_id)
            .values(fixed_personas=personas)
        )

        self.session.execute(statement)
        self.session.commit()

    def get_fixed_answers(self, test_id: UUID4) -> list[str] | None:
        statement = select(TestSetupTable).where(TestSetupTable.id == test_id)
        test = self.session.execute(statement).scalar_one()
        return test.fixed_answers

    def update_fixed_answers(self, test_id: UUID4, answers: list[str]):
        statement = (
            update(TestSetupTable)
            .where(TestSetupTable.id == test_id)
            .values(fixed_answers=answers)
        )
        self.session.execute(statement)
        self.session.commit()

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

    # ==================== Experiment Methods ====================

    def create_experiment(
        self, experiment: ExperimentCreate, user_id: UUID4
    ) -> ExperimentPublic:
        new_experiment = ExperimentTable(title=experiment.title, user_id=user_id)
        self.session.add(new_experiment)
        self.session.flush()

        for project in experiment.projects:
            experiment_project = ExperimentProjectTable(
                experiment_id=new_experiment.id,
                project_id=project.project_id,
                weight=project.weight,
            )
            self.session.add(experiment_project)

        self.session.commit()
        self.session.refresh(new_experiment)

        return self._to_experiment_public(new_experiment)

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

    def delete_experiment(self, experiment_id: UUID4, user_id: UUID4) -> None:
        """Delete an experiment owned by `user_id`, unlinking its projects and
        interviews.

        Interviews are kept and their reference cleared, matching the SET NULL
        that `interview.experiment_id` declares; the project links are deleted.
        Both are done explicitly because the cascade does not fire while
        foreign keys are unenforced -- see `delete_test_setup`.
        """
        owned = select(ExperimentTable.id).where(
            ExperimentTable.id == experiment_id,
            ExperimentTable.user_id == user_id,
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

    def _to_experiment_public(self, experiment: ExperimentTable) -> ExperimentPublic:
        """Convert an ExperimentTable to ExperimentPublic with projects."""
        projects = [
            ExperimentProjectPublic(
                id=ep.id,
                project_id=ep.project_id,
                weight=ep.weight,
                added_at=ep.added_at,
            )
            for ep in experiment.experiment_projects
        ]

        return ExperimentPublic(
            id=experiment.id,
            title=experiment.title,
            user_id=experiment.user_id,
            created_at=experiment.created_at,
            status=experiment.status,
            projects=projects,
        )
