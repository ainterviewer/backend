import asyncio
import uuid
from typing import Any, Literal, Optional, overload

from pydantic import UUID4
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ainterviewer.agents.config import AgentConfigs
from ainterviewer.agents.prompts.models import Prompts
from ainterviewer.config import InterviewConfig
from ainterviewer.interview_guides import InterviewGuide, Question
from ainterviewer.interview_guides.extra import Consent, Welcome
from ainterviewer.interview_guides.sections import QuestionSection
from ainterviewer.interview_guides.translate import (
    translate_consent,
    translate_interview_guide,
    translate_welcome,
)
from ainterviewer.types import LanguageCode, LanguageDict
from ainterviewer.utils import get_language_dict

from ...types import CollaboratorRole, ExternalParam, ProjectStatus, Scope
from ..models import (
    Collaborator,
    CollaboratorPublic,
    ProjectFolderPublic,
    ProjectFolderWithProjects,
    ProjectLocalizationPublic,
    ProjectPublic,
    ProjectPublicWithTests,
)
from ..tables import (
    CollaboratorTable,
    InterviewTable,
    ProjectFolderTable,
    ProjectLocalizationTable,
    ProjectTable,
    TestSetupTable,
    UserTable,
)
from ..types import InterviewType
from .base import BaseRepository

TRANSLATION_MODEL = "openai:gpt-5.4-mini"


class ProjectRepository(BaseRepository):
    """Repository for Project, Folder, and Localization operations."""

    # ==================== Folder Methods ====================

    def create_folder(
        self,
        title: str,
        user_id: UUID4,
        collaborators: list[Collaborator] | None = None,
    ) -> ProjectFolderPublic:
        folder = ProjectFolderTable(title=title)
        self.session.add(folder)
        self.session.flush()

        collaborator = CollaboratorTable(
            folder_id=folder.id,
            user_id=user_id,
            role=CollaboratorRole.ADMIN,
            added_by_id=user_id,
        )
        self.session.add(collaborator)

        if collaborators:
            # Find users by email
            stmt = select(UserTable).where(
                UserTable.email.in_(
                    collaborator.email for collaborator in collaborators
                )
            )
            users = self.session.execute(stmt).scalars().all()

            for user, collaborator in zip(users, collaborators):
                collab = CollaboratorTable(
                    folder_id=folder.id,
                    user_id=user.id,
                    role=collaborator.role,
                    added_by_id=user_id,
                )
                self.session.add(collab)

        self.session.commit()
        self.session.refresh(folder)

        return ProjectFolderPublic.model_validate(folder)

    def get_folders(
        self, user_id: UUID4, with_projects: bool = False
    ) -> list[ProjectFolderPublic | ProjectFolderWithProjects]:
        statement = (
            select(ProjectFolderTable)
            .join(CollaboratorTable)
            .where(CollaboratorTable.user_id == user_id)
        )

        folders = self.session.execute(statement).scalars().all()

        if with_projects:
            result = []

            for folder in folders:
                folder_dict = ProjectFolderWithProjects.model_validate(folder)
                folder_dict.projects = self._get_projects(
                    self.session,
                    folder.id,
                    include_interview_count=True,
                )
                result.append(folder_dict)

            return [
                ProjectFolderWithProjects.model_validate(folder) for folder in result
            ]

        return [ProjectFolderPublic.model_validate(folder) for folder in folders]

    def get_folder(self, folder_id: UUID4) -> ProjectFolderPublic:
        statement = select(ProjectFolderTable).where(ProjectFolderTable.id == folder_id)
        folder = self.session.execute(statement).scalar_one()
        return ProjectFolderPublic.model_validate(folder)

    def update_folder(self, folder_id: UUID4, title: str) -> ProjectFolderPublic:
        statement = (
            update(ProjectFolderTable)
            .where(ProjectFolderTable.id == folder_id)
            .values(title=title)
            .returning(ProjectFolderTable)
        )
        folder = self.session.execute(statement).scalar_one()
        self.session.commit()

        return ProjectFolderPublic.model_validate(folder)

    def delete_folder(self, folder_id: UUID4):
        statement = select(ProjectFolderTable).where(ProjectFolderTable.id == folder_id)
        folder = self.session.execute(statement).scalar_one()
        self.session.delete(folder)
        self.session.commit()

    # ================== Collaborator Methods ==================

    def add_collaborator(
        self,
        folder_id: UUID4,
        email: str,
        role: CollaboratorRole,
        added_by_id: UUID4,
    ) -> CollaboratorPublic:
        # Find user by email
        statement = select(UserTable).where(UserTable.email == email)
        user = self.session.execute(statement).scalar_one_or_none()
        if not user:
            raise ValueError(f"User with email {email} not found")

        collab = CollaboratorTable(
            folder_id=folder_id,
            user_id=user.id,
            role=role,
            added_by_id=added_by_id,
        )
        self.session.add(collab)
        self.session.commit()
        self.session.refresh(collab)
        return CollaboratorPublic.model_validate(collab)

    def remove_collaborator(self, folder_id: UUID4, user_id: UUID4):
        statement = delete(CollaboratorTable).where(
            CollaboratorTable.folder_id == folder_id,
            CollaboratorTable.user_id == user_id,
        )
        self.session.execute(statement)
        self.session.commit()

    def update_collaborator_role(
        self, folder_id: UUID4, user_id: UUID4, role: CollaboratorRole
    ) -> CollaboratorPublic:
        statement = (
            update(CollaboratorTable)
            .where(
                CollaboratorTable.folder_id == folder_id,
                CollaboratorTable.user_id == user_id,
            )
            .values(role=role)
            .returning(CollaboratorTable)
        )
        collab = self.session.execute(statement).scalar_one()
        self.session.commit()

        return CollaboratorPublic.model_validate(collab)

    def get_collaborators(self, folder_id: UUID4) -> list[CollaboratorPublic]:
        statement = select(CollaboratorTable).where(
            CollaboratorTable.folder_id == folder_id
        )
        collabs = self.session.execute(statement).scalars().all()
        return [CollaboratorPublic.model_validate(c) for c in collabs]

    # ==================== Project Methods ====================

    def create_project(
        self,
        folder_id: UUID4,
        title: str,
        owner_id: UUID4,
        interview_config: Optional[InterviewConfig] = None,
        interview_guide_content: Optional[InterviewGuide] = None,
        agent_configs: Optional[AgentConfigs] = None,
        prompts: Optional[Prompts] = None,
        project_id: Optional[UUID4] = None,
    ) -> UUID4:
        project_kwargs = {}

        if interview_config:
            project_kwargs["config"] = interview_config
        if project_id:
            project_kwargs["id"] = project_id

        project = ProjectTable(
            folder_id=folder_id,
            title=title,
            owner_id=owner_id,
            **project_kwargs,
        )

        self.session.add(project)
        self.session.flush()

        localization_kwargs = {}
        if interview_guide_content:
            localization_kwargs["interview_guide"] = interview_guide_content
        else:
            # New projects start with a guide containing one empty question so
            # the editor has something to fill in.
            localization_kwargs["interview_guide"] = InterviewGuide(
                question_sections=[
                    QuestionSection(
                        description="",
                        questions=[Question(main_question="")],
                    )
                ]
            )
        if agent_configs:
            localization_kwargs["agent_configs"] = agent_configs
        if prompts:
            localization_kwargs["prompts"] = prompts

        default_localization = ProjectLocalizationTable(
            project_id=project.id,
            language=project.config.default_language,
            **localization_kwargs,
        )
        self.session.add(default_localization)

        self.session.commit()

        return project.id

    def clone_project(
        self,
        project_id: UUID4,
        owner_id: UUID4,
    ) -> ProjectPublic:
        """
        Copies a project with all its configurations (localizations, guides, agent configs, prompts)
        but not interviews, messages, or test runs.
        """
        # 1. Load the original project
        orig_project = self.session.execute(
            select(ProjectTable).where(ProjectTable.id == project_id)
        ).scalar_one()

        # 2. Create the new project
        base_title = orig_project.title
        original_config = orig_project.config
        original_status = orig_project.status

        copy_attempt = 0
        new_project: ProjectTable | None = None

        while True:
            if copy_attempt == 0:
                clone_title = f"{base_title} (Copy)"
            else:
                clone_title = f"{base_title} (Copy {copy_attempt + 1})"

            new_project = ProjectTable(
                folder_id=orig_project.folder_id,
                title=clone_title,
                config=original_config,
                status=original_status,
                owner_id=owner_id,
            )

            self.session.add(new_project)

            try:
                self.session.flush()
                break
            except IntegrityError as exc:
                self.session.rollback()
                constraint_name = getattr(
                    getattr(exc.orig, "diag", None), "constraint_name", None
                )
                if constraint_name and constraint_name != "title":
                    raise
                copy_attempt += 1
                continue

        assert new_project is not None

        # 3. Copy all project localizations
        localizations = (
            self.session.execute(
                select(ProjectLocalizationTable).where(
                    ProjectLocalizationTable.project_id == project_id
                )
            )
            .scalars()
            .all()
        )

        for loc in localizations:
            new_loc = ProjectLocalizationTable(
                project_id=new_project.id,
                language=loc.language,
                interview_guide=loc.interview_guide,
                prompts=loc.prompts,
                agent_configs=loc.agent_configs,
                consent=loc.consent,
                welcome=loc.welcome,
                participant_email_subject=loc.participant_email_subject,
                participant_email_template=loc.participant_email_template,
                participant_reminder_email_subject=loc.participant_reminder_email_subject,
                participant_reminder_email_template=loc.participant_reminder_email_template,
            )
            self.session.add(new_loc)

        # 4. Copy all test setups (configurations only, not their runs/interviews)
        test_setups = (
            self.session.execute(
                select(TestSetupTable).where(TestSetupTable.project_id == project_id)
            )
            .scalars()
            .all()
        )

        for setup in test_setups:
            new_setup = TestSetupTable(
                project_id=new_project.id,
                name=setup.name,
                type=setup.type,
                language=setup.language,
                n_interviews=setup.n_interviews,
                answering_model=setup.answering_model,
                delay_before_answers=setup.delay_before_answers,
                background_info=setup.background_info,
                fixed_answers=setup.fixed_answers,
                fixed_personas=setup.fixed_personas,
            )
            self.session.add(new_setup)

        self.session.commit()

        return ProjectPublic.model_validate(new_project)

    def get_projects(
        self,
        folder_id: UUID4,
        include_available_languages: bool = False,
        include_interview_count: bool = False,
    ) -> list[ProjectPublic]:
        return self._get_projects(
            self.session,
            folder_id,
            include_available_languages,
            include_interview_count,
        )

    def _get_projects(
        self,
        session: Session,
        folder_id: UUID4,
        include_available_languages: bool = False,
        include_interview_count: bool = False,
    ) -> list[ProjectPublic]:
        statement = select(ProjectTable).where(ProjectTable.folder_id == folder_id)
        projects = session.execute(statement).scalars().all()
        result = []
        for project in projects:
            # Convert to dict for manipulation
            project_dict = ProjectPublic.model_validate(project).model_dump()

            # Add optional computed fields
            if include_available_languages:
                project_dict["available_languages"] = (
                    self.get_available_languages_optimized(project.id)
                )

            if include_interview_count:
                project_dict["n_interviews"] = self.get_interview_count_optimized(
                    project.id
                )

            result.append(ProjectPublic.model_validate(project_dict))

        return result

    @overload
    def get_project(
        self,
        project_id: UUID4,
        *,
        with_interviews: bool = False,
        with_tests: Literal[True],
    ) -> ProjectPublicWithTests: ...

    @overload
    def get_project(
        self,
        project_id: UUID4,
        *,
        with_interviews: bool = False,
        with_tests: Literal[False] = False,
    ) -> ProjectPublic: ...

    def get_project(
        self,
        project_id: UUID4,
        *,
        with_interviews: bool = False,
        with_tests: bool = False,
    ) -> ProjectPublic | ProjectPublicWithTests:
        statement = select(ProjectTable).where(
            ProjectTable.id == project_id,
        )

        project = self.session.execute(statement).scalar_one()

        if with_interviews:
            project.interviews

        if with_tests:
            project.tests
            if project.tests is None:
                project.tests = []

        public_project = ProjectPublic.model_validate(project)

        public_project.available_languages = self.get_available_languages_optimized(
            project.id
        )

        if with_tests is True:
            if public_project.tests is None:
                public_project.tests = []
        else:
            public_project.tests = None

        return public_project

    def change_project_status(
        self,
        project_id: UUID4,
        status: ProjectStatus,
    ):
        statement = (
            update(ProjectTable)
            .where(ProjectTable.id == project_id)
            .values(status=status)
        )
        self.session.execute(statement)
        self.session.commit()

    def update_project_title(
        self,
        project_id: UUID4,
        title: str,
    ):
        statement = (
            update(ProjectTable)
            .where(ProjectTable.id == project_id)
            .values(title=title)
        )
        self.session.execute(statement)
        self.session.commit()

    def delete_project(self, project_id: UUID4):
        # FIXME: Update permissions to collab
        statement = select(ProjectTable).where(
            ProjectTable.id == project_id,
        )
        project = self.session.execute(statement).scalar_one()
        self.session.delete(project)
        self.session.commit()

    # ==================== Localization Methods ====================

    def get_project_localization(
        self,
        project_id: UUID4,
        language: LanguageCode,
    ) -> ProjectLocalizationPublic:
        statement = select(ProjectLocalizationTable).where(
            ProjectLocalizationTable.project_id == project_id,
            ProjectLocalizationTable.language == language,
        )

        project_localization = self.session.execute(statement).scalar_one()

        return ProjectLocalizationPublic.model_validate(project_localization)

    async def add_project_language(
        self, project_id: UUID4, language: LanguageCode, translate: bool
    ):
        statement = select(ProjectTable).where(ProjectTable.id == project_id)
        project = self.session.execute(statement).scalar_one()
        await self._add_localization(project, language, translate)
        self.session.add(project)
        self.session.commit()

        return self.get_available_languages_optimized(project.id)

    def remove_project_language(
        self, project_id: UUID4, language: LanguageCode
    ) -> list[LanguageDict]:
        delete_statement = delete(ProjectLocalizationTable).where(
            ProjectLocalizationTable.project_id == project_id,
            ProjectLocalizationTable.language == language,
        )
        self.session.execute(delete_statement)
        self.session.commit()

        return self.get_available_languages_optimized(project_id)

    async def _add_localization(
        self, project: ProjectTable, language: LanguageCode, translate: bool
    ):
        """Add a new localization to the project"""
        # Check if localization already exists
        if any(loc.language == language for loc in project.localizations):
            return

        default_loc = self._get_default_localization(project)

        consent, welcome, interview_guide = (
            default_loc.consent,
            default_loc.welcome,
            default_loc.interview_guide,
        )

        if translate:

            async def _none():
                return None

            target_language = get_language_dict(language)["name"]
            tasks = []
            if interview_guide is not None:
                tasks.append(
                    translate_interview_guide(
                        interview_guide,
                        target_language=target_language,
                        model=TRANSLATION_MODEL,
                    )
                )
            if consent is not None:
                tasks.append(
                    translate_consent(
                        consent,
                        target_language=target_language,
                        model=TRANSLATION_MODEL,
                    )
                )
            if welcome is not None:
                tasks.append(
                    translate_welcome(
                        welcome,
                        target_language=target_language,
                        model=TRANSLATION_MODEL,
                    )
                )
            tasks += [_none(), _none(), _none()]

            interview_guide, consent, welcome, *_ = await asyncio.gather(*tasks)

        new_loc = ProjectLocalizationTable(
            project_id=project.id,
            language=language,
            consent=consent,
            welcome=welcome,
            interview_guide=interview_guide,
            prompts=default_loc.prompts,
            agent_configs=default_loc.agent_configs,
        )
        self.session.add(new_loc)
        self.session.flush()

    def _get_localization(
        self, project: ProjectTable, language: LanguageCode
    ) -> ProjectLocalizationTable:
        """Get localization for a specific language, falls back to default if not found"""
        # Try to find the requested language
        for loc in project.localizations:
            if loc.language == language:
                return loc

        # Fallback to default language
        for loc in project.localizations:
            if loc.language == project.config.default_language:
                return loc

        # This should not happen if data integrity is maintained
        raise ValueError(f"No localization found for project {project.id}")

    def _get_default_localization(
        self, project: ProjectTable
    ) -> ProjectLocalizationTable:
        """Get the default localization for this project."""
        return self._get_localization(project, project.config.default_language)

    def get_available_languages(self, project: ProjectTable) -> list[LanguageDict]:
        """Get list of available languages for the project"""
        return list(
            sorted(
                (
                    get_language_dict(localization.language)
                    for localization in project.localizations
                ),
                key=lambda lan: lan["name"],
            )
        )

    def get_available_languages_optimized(
        self, project_id: uuid.UUID
    ) -> list[LanguageDict]:
        """
        Get available languages without loading the full project.
        Optimized query that only fetches language codes.
        """
        languages = (
            self.session.query(ProjectLocalizationTable.language)
            .filter(ProjectLocalizationTable.project_id == project_id)
            .all()
        )
        return list(
            sorted(
                (get_language_dict(lang[0]) for lang in languages),
                key=lambda lan: lan["name"],
            )
        )

    def get_interview_count_optimized(
        self, project_id: uuid.UUID, exclude_tests: bool = True
    ) -> int:
        """
        Get interview count without loading the full project.
        Optimized query that only counts interviews.
        """

        statement = self.session.query(func.count(InterviewTable.id)).filter(
            InterviewTable.project_id == project_id
        )

        if exclude_tests:
            statement = statement.filter(
                InterviewTable.type == InterviewType.DISTRIBUTED
            )

        return statement.scalar() or 0

    # ==================== Interview Guide / Config Methods ====================

    def update_interview_guide(
        self,
        project_id: UUID4,
        interview_guide_content: InterviewGuide,
        language: LanguageCode,
    ):
        statement = (
            update(ProjectLocalizationTable)
            .where(
                ProjectLocalizationTable.project_id == project_id,
                ProjectLocalizationTable.language == language,
            )
            .values(interview_guide=interview_guide_content)
        )
        self.session.execute(statement)
        self.session.commit()

    def update_interview_config(
        self,
        project_id: UUID4,
        interview_config: InterviewConfig,
    ):
        statement = (
            update(ProjectTable)
            .where(ProjectTable.id == project_id)
            .values(config=interview_config)
        )
        self.session.execute(statement)
        self.session.commit()

    def get_external_param_values_for_interview(
        self,
        interview_id: UUID4,
    ) -> dict[str, Any] | None:
        statement = select(InterviewTable).where(InterviewTable.id == interview_id)
        interview = self.session.execute(statement).scalar_one()

        return interview.external_params

    def update_external_params(
        self,
        project_id: UUID4,
        external_params: list[ExternalParam],
    ):
        statement = (
            update(ProjectTable)
            .where(ProjectTable.id == project_id)
            .values(external_params=external_params)
        )
        self.session.execute(statement)
        self.session.commit()

    def update_agent_configs(
        self,
        project_id: UUID4,
        language: LanguageCode,
        agent_configs: AgentConfigs,
    ):
        # FIXME: Update permissions to collab
        statement = (
            update(ProjectLocalizationTable)
            .where(
                ProjectLocalizationTable.project_id == project_id,
                ProjectLocalizationTable.language == language,
            )
            .values(agent_configs=agent_configs)
        )
        self.session.execute(statement)
        self.session.commit()

    def set_prompts(
        self,
        project_id: UUID4,
        language: LanguageCode,
        prompts: Prompts,
    ):
        # FIXME: Update permissions to collab

        self.session.execute(
            update(ProjectLocalizationTable)
            .where(
                ProjectLocalizationTable.project_id == project_id,
                ProjectLocalizationTable.language == language,
            )
            .values(prompts=prompts)
        )

        self.session.commit()

    # ==================== Consent / Welcome Methods ====================

    def get_consent(self, project_id: UUID4, language: LanguageCode) -> Consent | None:
        statement = select(ProjectLocalizationTable).where(
            ProjectLocalizationTable.project_id == project_id,
            ProjectLocalizationTable.language == language,
        )
        return self.session.execute(statement).scalar_one().consent

    def update_consent(
        self, project_id: UUID4, consent: Consent, language: LanguageCode
    ):
        statement = (
            update(ProjectLocalizationTable)
            .where(
                ProjectLocalizationTable.project_id == project_id,
                ProjectLocalizationTable.language == language,
            )
            .values(consent=consent)
        )
        self.session.execute(statement)
        self.session.commit()

    def get_welcome(self, project_id: UUID4, language: LanguageCode) -> Welcome | None:
        statement = select(ProjectLocalizationTable).where(
            ProjectLocalizationTable.project_id == project_id,
            ProjectLocalizationTable.language == language,
        )
        return self.session.execute(statement).scalar_one().welcome

    def update_welcome(
        self, project_id: UUID4, welcome: Welcome, language: LanguageCode
    ):
        statement = (
            update(ProjectLocalizationTable)
            .where(
                ProjectLocalizationTable.project_id == project_id,
                ProjectLocalizationTable.language == language,
            )
            .values(welcome=welcome)
        )
        self.session.execute(statement)
        self.session.commit()

    # ============ Participant Email Template Methods ============

    def get_participant_email_template(
        self, project_id: UUID4, language: LanguageCode
    ) -> tuple[str | None, str | None]:
        """Return (subject, template) for the given localization."""
        statement = select(
            ProjectLocalizationTable.participant_email_subject,
            ProjectLocalizationTable.participant_email_template,
        ).where(
            ProjectLocalizationTable.project_id == project_id,
            ProjectLocalizationTable.language == language,
        )
        row = self.session.execute(statement).one()
        return row[0], row[1]

    def set_participant_email_template(
        self,
        project_id: UUID4,
        language: LanguageCode,
        subject: str | None,
        template: str | None,
    ) -> None:
        statement = (
            update(ProjectLocalizationTable)
            .where(
                ProjectLocalizationTable.project_id == project_id,
                ProjectLocalizationTable.language == language,
            )
            .values(
                participant_email_subject=subject,
                participant_email_template=template,
            )
        )
        self.session.execute(statement)
        self.session.commit()

    def get_participant_email_templates_ordered(
        self, project_id: UUID4
    ) -> list[tuple[LanguageCode, str | None, str | None]]:
        """Return (language, subject, template) for every localization, with
        the project's default language first."""
        project = self.session.execute(
            select(ProjectTable).where(ProjectTable.id == project_id)
        ).scalar_one()
        default_lang = project.config.default_language

        rows = self.session.execute(
            select(
                ProjectLocalizationTable.language,
                ProjectLocalizationTable.participant_email_subject,
                ProjectLocalizationTable.participant_email_template,
            ).where(ProjectLocalizationTable.project_id == project_id)
        ).all()
        ordered = sorted(
            (tuple(r) for r in rows),
            key=lambda r: (r[0] != default_lang, r[0]),
        )
        return [(lang, subj, tmpl) for (lang, subj, tmpl) in ordered]

    # ====== Participant Reminder Email Template Methods ======

    def get_participant_reminder_email_template(
        self, project_id: UUID4, language: LanguageCode
    ) -> tuple[str | None, str | None]:
        """Return (subject, template) of the reminder email for the given localization."""
        statement = select(
            ProjectLocalizationTable.participant_reminder_email_subject,
            ProjectLocalizationTable.participant_reminder_email_template,
        ).where(
            ProjectLocalizationTable.project_id == project_id,
            ProjectLocalizationTable.language == language,
        )
        row = self.session.execute(statement).one()
        return row[0], row[1]

    def set_participant_reminder_email_template(
        self,
        project_id: UUID4,
        language: LanguageCode,
        subject: str | None,
        template: str | None,
    ) -> None:
        statement = (
            update(ProjectLocalizationTable)
            .where(
                ProjectLocalizationTable.project_id == project_id,
                ProjectLocalizationTable.language == language,
            )
            .values(
                participant_reminder_email_subject=subject,
                participant_reminder_email_template=template,
            )
        )
        self.session.execute(statement)
        self.session.commit()

    def get_participant_reminder_email_templates_ordered(
        self, project_id: UUID4
    ) -> list[tuple[LanguageCode, str | None, str | None]]:
        """Return (language, subject, template) of the reminder email for every
        localization, with the project's default language first."""
        project = self.session.execute(
            select(ProjectTable).where(ProjectTable.id == project_id)
        ).scalar_one()
        default_lang = project.config.default_language

        rows = self.session.execute(
            select(
                ProjectLocalizationTable.language,
                ProjectLocalizationTable.participant_reminder_email_subject,
                ProjectLocalizationTable.participant_reminder_email_template,
            ).where(ProjectLocalizationTable.project_id == project_id)
        ).all()
        ordered = sorted(
            (tuple(r) for r in rows),
            key=lambda r: (r[0] != default_lang, r[0]),
        )
        return [(lang, subj, tmpl) for (lang, subj, tmpl) in ordered]

    # ==================== Authorization Methods ====================

    def get_user_role_on_folder(
        self, user_id: UUID4, folder_id: UUID4
    ) -> CollaboratorRole | None:
        """Get user's role on a folder, or None if no access."""
        statement = select(CollaboratorTable).where(
            CollaboratorTable.folder_id == folder_id,
            CollaboratorTable.user_id == user_id,
        )
        collab = self.session.execute(statement).scalar_one_or_none()
        return collab.role if collab else None

    def get_user_role_on_project(
        self, user_id: UUID4, project_id: UUID4
    ) -> CollaboratorRole | None:
        """Get user's role on a project via its folder."""
        statement = (
            select(CollaboratorTable.role)
            .join(
                ProjectFolderTable, CollaboratorTable.folder_id == ProjectFolderTable.id
            )
            .join(ProjectTable, ProjectTable.folder_id == ProjectFolderTable.id)
            .where(
                ProjectTable.id == project_id,
                CollaboratorTable.user_id == user_id,
            )
        )
        return self.session.execute(statement).scalar_one_or_none()

    def is_project_owner(self, user_id: UUID4, project_id: UUID4) -> bool:
        """Check if a user is the owner of a project."""
        result = self.session.execute(
            select(ProjectTable.id).where(
                ProjectTable.id == project_id,
                ProjectTable.owner_id == user_id,
            )
        ).scalar_one_or_none()
        return result is not None

    def is_project_owner_demo_user(self, project_id: UUID4):
        project = self.session.execute(
            select(ProjectTable).where(ProjectTable.id == project_id)
        ).scalar_one()

        return project.owner.scope == Scope.DEMO
