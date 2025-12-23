import uuid
from typing import Literal, Optional, overload

from pydantic import UUID4
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ainterviewer.config import AgentConfigs, InterviewConfig
from ainterviewer.interview_guides import InterviewGuide
from ainterviewer.interview_guides.extra import Consent, Welcome
from ainterviewer.prompts.models import Prompts
from ainterviewer.types import LanguageCode, LanguageDict
from ainterviewer.utils import get_language_dict

from ...api.request_models import PromptsUpdateRequest
from ...types import ProjectStatus
from ..models import (
    CollaboratorCreate,
    CollaboratorPublic,
    ProjectFolderPublic,
    ProjectFolderWithProjects,
    ProjectLocalizationPublic,
    ProjectPublic,
    ProjectPublicWithTests,
    Collaborator,
)
from ..tables import (
    CollaboratorTable,
    InterviewTable,
    ProjectFolderTable,
    ProjectLocalizationTable,
    ProjectTable,
    UserTable,
)
from ..types import CollaboratorRole
from .base import BaseRepository


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
        statement = select(ProjectFolderTable).where(ProjectFolderTable.id == folder_id)
        folder = self.session.execute(statement).scalar_one()
        folder.title = title
        self.session.add(folder)
        self.session.commit()
        self.session.refresh(folder)
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
        statement = select(CollaboratorTable).where(
            CollaboratorTable.folder_id == folder_id,
            CollaboratorTable.user_id == user_id,
        )
        collab = self.session.execute(statement).scalar_one()
        collab.role = role
        self.session.add(collab)
        self.session.commit()
        self.session.refresh(collab)
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
        interview_config: Optional[InterviewConfig],
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
            **project_kwargs,
        )

        self.session.add(project)
        self.session.flush()

        localization_kwargs = {}
        if interview_guide_content:
            localization_kwargs["interview_guide"] = interview_guide_content
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
            )
            self.session.add(new_loc)

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
        with_localizations: bool = False,
    ) -> ProjectPublicWithTests: ...

    @overload
    def get_project(
        self,
        project_id: UUID4,
        *,
        with_interviews: bool = False,
        with_tests: Literal[False] = False,
        with_localizations: bool = False,
    ) -> ProjectPublic: ...

    def get_project(
        self,
        project_id: UUID4,
        *,
        with_interviews: bool = False,
        with_tests: bool = False,
        with_localizations: bool = False,
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

        if with_localizations:
            project.localizations

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
        statement = select(ProjectTable).where(
            ProjectTable.id == project_id,
        )

        project = self.session.execute(statement).scalar_one()
        project.status = status

        self.session.add(project)
        self.session.commit()

    def update_project_title(
        self,
        project_id: UUID4,
        title: str,
    ):
        statement = select(ProjectTable).where(
            ProjectTable.id == project_id,
        )

        project = self.session.execute(statement).scalar_one()
        project.title = title

        self.session.add(project)
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

    def add_project_language(self, project_id: UUID4, language: LanguageCode):
        statement = select(ProjectTable).where(ProjectTable.id == project_id)
        project = self.session.execute(statement).scalar_one()
        self._add_localization(project, language)
        self.session.add(project)
        self.session.commit()

        return self.get_available_languages_optimized(project.id)

    def remove_project_language(self, project_id: UUID4, language: LanguageCode):
        delete_statement = delete(ProjectLocalizationTable).where(
            ProjectLocalizationTable.project_id == project_id,
            ProjectLocalizationTable.language == language,
        )
        self.session.execute(delete_statement)

        statement = select(ProjectTable).where(ProjectTable.id == project_id)
        project = self.session.execute(statement).scalar_one()
        self.session.commit()

        return project.available_languages

    def _add_localization(self, project: ProjectTable, language: LanguageCode):
        """Add a new localization to the project"""
        # Check if localization already exists
        if any(loc.language == language for loc in project.localizations):
            return

        default_loc = self._get_default_localization(project)
        new_loc = ProjectLocalizationTable(
            project_id=project.id,
            language=language,
            interview_guide=default_loc.interview_guide,
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

    def get_interview_count_optimized(self, project_id: uuid.UUID) -> int:
        """
        Get interview count without loading the full project.
        Optimized query that only counts interviews.
        """
        return (
            self.session.query(func.count(InterviewTable.id))
            .filter(InterviewTable.project_id == project_id)
            .scalar()
        ) or 0

    # ==================== Interview Guide / Config Methods ====================

    def update_interview_guide(
        self,
        project_id: UUID4,
        interview_guide_content: InterviewGuide,
        language: LanguageCode,
    ):
        statement = select(ProjectLocalizationTable).where(
            ProjectLocalizationTable.project_id == project_id,
            ProjectLocalizationTable.language == language,
        )
        project_localization = self.session.execute(statement).scalar_one()
        project_localization.interview_guide = interview_guide_content

        self.session.add(project_localization)
        self.session.commit()

    def update_interview_config(
        self,
        project_id: UUID4,
        interview_config: InterviewConfig,
    ):
        statement = select(ProjectTable).where(ProjectTable.id == project_id)
        project = self.session.execute(statement).scalar_one()
        project.config = interview_config
        self.session.add(project)
        self.session.commit()

    def update_agent_configs(
        self,
        project_id: UUID4,
        language: LanguageCode,
        agent_configs: AgentConfigs,
    ):
        # FIXME: Update permissions to collab
        statement = select(ProjectLocalizationTable).where(
            ProjectLocalizationTable.project_id == project_id,
            ProjectLocalizationTable.language == language,
        )
        project_localization = self.session.execute(statement).scalar_one()
        project_localization.agent_configs = agent_configs
        self.session.add(project_localization)
        self.session.commit()

    def set_prompts(
        self,
        project_id: UUID4,
        language: LanguageCode,
        prompts: Prompts,
    ):
        # FIXME: Update permissions to collab
        statement = select(ProjectLocalizationTable).where(
            ProjectLocalizationTable.project_id == project_id,
            ProjectLocalizationTable.language == language,
        )
        project_localization = self.session.execute(statement).scalar_one()
        project_localization.prompts = prompts
        self.session.add(project_localization)
        self.session.commit()

    def update_prompts(
        self,
        project_id: UUID4,
        language: LanguageCode,
        prompts: PromptsUpdateRequest,
    ):
        # FIXME: Update permissions to collab
        statement = select(ProjectLocalizationTable).where(
            ProjectLocalizationTable.project_id == project_id,
            ProjectLocalizationTable.language == language,
        )
        project_localization = self.session.execute(statement).scalar_one()

        existing_prompts = project_localization.prompts

        # Merge updates recursively
        updated_data = existing_prompts.model_dump()
        new_data = prompts.model_dump(exclude_unset=True)

        for agent, value in new_data.items():
            if isinstance(value, dict):
                updated_data[agent].update(value)
            else:
                updated_data[agent] = value

        # Recreate full Prompts model
        project_localization.prompts = Prompts(**updated_data)
        self.session.add(project_localization)
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
        statement = select(ProjectLocalizationTable).where(
            ProjectLocalizationTable.project_id == project_id,
            ProjectLocalizationTable.language == language,
        )
        project_localization = self.session.execute(statement).scalar_one()
        project_localization.consent = consent

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
        statement = select(ProjectLocalizationTable).where(
            ProjectLocalizationTable.project_id == project_id,
            ProjectLocalizationTable.language == language,
        )
        project_localization = self.session.execute(statement).scalar_one()
        project_localization.welcome = welcome

        self.session.commit()
