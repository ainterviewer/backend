import asyncio
import logging
import uuid
from collections.abc import Iterable, Sequence
from typing import Any, Literal, overload

from pydantic import UUID4, BaseModel, ValidationError
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError, NoResultFound
from sqlalchemy.orm import Session

from ainterviewer.agents.config import AgentConfigs
from ainterviewer.config import InterviewConfig
from ainterviewer.interview_guides import (
    InterviewGuide,
    LocalizedConsent,
    LocalizedInterviewGuide,
    LocalizedWelcome,
)
from ainterviewer.interview_guides.localized import (
    LocalizedQuestion,
    LocalizedQuestionSection,
)
from ainterviewer.interview_guides.translate import translate_missing
from ainterviewer.types import LanguageCode
from ainterviewer.utils import get_language_dict

from ...collab.guide_doc import merge_guide_data
from ...types import (
    CollaboratorRole,
    ExternalParam,
    ParticipantEmail,
    ParticipantEmails,
    ProjectStatus,
    Scope,
)
from ..models import (
    Collaborator,
    CollaboratorPublic,
    ProjectFolderPublic,
    ProjectFolderWithProjects,
    ProjectLanguage,
    ProjectLocalizationPublic,
    ProjectPublic,
    ProjectPublicWithTests,
)
from ..tables import (
    CollaboratorTable,
    InterviewTable,
    ProjectContentTable,
    ProjectContentVersionTable,
    ProjectFolderTable,
    ProjectTable,
    TestSetupTable,
    UserTable,
)
from ..types import ContentPart, InterviewType
from .base import BaseRepository
from .errors import ContentRevisionError, NotPublishedError, ProjectLanguageError
from .permissions import can_moderate_project

logger = logging.getLogger(__name__)

TRANSLATION_MODEL = "openai:gpt-5.4-mini"

#: Which of a project's content to read: the draft the editor edits, the
#: version respondents get, or a version by id. See `VersionRepository`.
type ContentSource = Literal["draft", "published"] | UUID4


def _sorted_project_languages(
    languages: Iterable[LanguageCode],
    default: LanguageCode,
    in_progress: Iterable[LanguageCode] = (),
) -> list[ProjectLanguage]:
    """Build the public language list, sorted by name."""
    in_progress = set(in_progress)
    return sorted(
        (
            ProjectLanguage(
                **get_language_dict(language),
                is_default=language == default,
                in_progress=language in in_progress,
            )
            for language in languages
        ),
        key=lambda lan: lan["name"],
    )


def render_default_guide(session: Session, project_id: UUID4) -> InterviewGuide | None:
    """The project's guide in its default language, None if it has none.

    For the analysis views, which label positions with the authored wording.
    """
    row = session.execute(
        select(ProjectContentTable.guide, ProjectContentTable.default_language).where(
            ProjectContentTable.project_id == project_id
        )
    ).one_or_none()
    return None if row is None else row.guide.render(row.default_language)


#: The parts `translate_missing` fills in.
_TRANSLATED = ("guide", "consent", "welcome")


def _dump(value: BaseModel | None) -> Any:
    return None if value is None else value.model_dump(mode="json")


def _merge_translation[T: BaseModel](
    before: Any, current: T | None, translated: T | None
) -> T | None:
    """`translated` (made from `before`) merged into `current`, what the
    column holds now: text an editor changed meanwhile is theirs, the rest
    gains the translation. A part cleared meanwhile stays cleared."""
    if current is None or translated is None:
        return current
    merged = merge_guide_data(before, _dump(current), _dump(translated))
    try:
        return type(translated).model_validate(merged)
    except ValidationError:
        # Edits since made the two incompatible; the editors' version stands
        # and translating again picks up what is still missing.
        logger.warning("Dropped a translation that no longer fits its content")
        return current


def _without_language[
    T: (LocalizedInterviewGuide, LocalizedConsent, LocalizedWelcome, ParticipantEmails)
](content: T, language: LanguageCode) -> T:
    """A copy of `content` with every text in `language` removed."""
    stripped = content.model_copy(deep=True)
    for _, text in stripped.texts():
        text.pop(language, None)
    return stripped


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
                    # The dashboard needs each project's default language to
                    # build its per-language links.
                    include_available_languages=True,
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

    def get_folder_project_ids(self, folder_id: UUID4) -> list[uuid.UUID]:
        """The projects a folder delete takes with it."""
        statement = select(ProjectTable.id).where(ProjectTable.folder_id == folder_id)
        return list(self.session.execute(statement).scalars())

    def get_owned_project_ids(self, user_id: UUID4) -> list[uuid.UUID]:
        """The projects a user delete takes with it."""
        statement = select(ProjectTable.id).where(ProjectTable.owner_id == user_id)
        return list(self.session.execute(statement).scalars())

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
        default_language: LanguageCode = "EN",
        interview_config: InterviewConfig | None = None,
        project_id: UUID4 | None = None,
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

        # New projects start with a guide containing one empty question so
        # the editor has something to fill in.
        self.session.add(
            ProjectContentTable(
                project_id=project.id,
                languages=[default_language],
                default_language=default_language,
                guide=LocalizedInterviewGuide(
                    question_sections=[
                        LocalizedQuestionSection(questions=[LocalizedQuestion()])
                    ]
                ),
            )
        )
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

        # 3. Copy the content, languages and all
        content = self._content(project_id)
        self.session.add(
            ProjectContentTable(
                project_id=new_project.id,
                languages=list(content.languages),
                default_language=content.default_language,
                languages_in_progress=list(content.languages_in_progress),
                guide=content.guide,
                consent=content.consent,
                welcome=content.welcome,
                emails=content.emails,
                prompt_overrides=dict(content.prompt_overrides),
                agent_configs=content.agent_configs,
            )
        )

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
            _ = project.interviews

        if with_tests:
            _ = project.tests
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

    def move_project(self, project_id: UUID4, folder_id: UUID4):
        """Move a project into another folder.

        Raises IntegrityError if the target folder already holds a project with
        the same title (the ("title", "folder_id") unique constraint).
        """
        statement = (
            update(ProjectTable)
            .where(ProjectTable.id == project_id)
            .values(folder_id=folder_id)
        )
        try:
            self.session.execute(statement)
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            raise

    def delete_project(self, project_id: UUID4):
        # FIXME: Update permissions to collab
        statement = select(ProjectTable).where(
            ProjectTable.id == project_id,
        )
        project = self.session.execute(statement).scalar_one()
        self.session.delete(project)
        self.session.commit()

    # ==================== Localization Methods ====================

    def _source(
        self, project_id: UUID4, source: ContentSource
    ) -> ProjectContentTable | ProjectContentVersionTable:
        """The draft, the published version or a given version of the
        project's content. Raises NotPublishedError for "published" when
        nothing is, and NoResultFound for a version that is not this
        project's."""
        if source == "draft":
            return self._content(project_id)
        if source == "published":
            version = self._content(project_id).published_version
            if version is None:
                raise NotPublishedError("This study has not been published yet.")
            return version
        return self.session.execute(
            select(ProjectContentVersionTable).where(
                ProjectContentVersionTable.id == source,
                ProjectContentVersionTable.project_id == project_id,
            )
        ).scalar_one()

    def get_project_localization(
        self,
        project_id: UUID4,
        language: LanguageCode,
        *,
        source: ContentSource = "draft",
        or_default: bool = False,
    ) -> ProjectLocalizationPublic:
        """The project as respondents in `language` get it: the content
        rendered in that language, untranslated text in the default's.

        `source` picks what is rendered: the draft (the editor's previews and
        test interviews' snapshots go through their version id), the
        published version (respondents) or a version by id.

        Raises NoResultFound if that content does not offer `language`, unless
        `or_default`, which renders its default language instead.
        """
        content = self._source(project_id, source)
        # Respondents are offered what is not in progress; a test, running the
        # draft or its snapshot, may try a language that still is.
        offered = content.offered if source == "published" else content.languages
        if or_default and language not in offered:
            language = content.default_language
        self._require_language(content, language, offered)
        fallback = [content.default_language]
        return ProjectLocalizationPublic(
            project_id=project_id,
            content_version_id=None
            if isinstance(content, ProjectContentTable)
            else content.id,
            language=language,
            interview_guide=content.guide.render(language, fallback),
            consent=None
            if content.consent is None
            else content.consent.render(language, fallback),
            welcome=None
            if content.welcome is None
            else content.welcome.render(language, fallback),
            prompt_overrides=dict(content.prompt_overrides),
            agent_configs=content.agent_configs,
        )

    @staticmethod
    def _require_language(
        content: ProjectContentTable | ProjectContentVersionTable,
        language: LanguageCode,
        offered: Iterable[LanguageCode] | None = None,
    ):
        if language not in (content.languages if offered is None else offered):
            raise NoResultFound(
                f"Project {content.project_id} does not offer {language}."
            )

    async def add_project_language(
        self, project_id: UUID4, language: LanguageCode, translate: bool
    ) -> list[ProjectLanguage]:
        """Offer `language`, machine-translating the content's text into it
        from the default language if `translate`. Adding one already offered
        changes nothing."""
        content = self._content(project_id)
        if language not in content.languages:
            content.languages = [*content.languages, language]
            # In progress until someone marks it ready: untranslated, or only
            # machine-translated, it should neither reach respondents nor
            # block publishing everything else.
            content.languages_in_progress = [*content.languages_in_progress, language]
            # Committed before translating: an LLM call holding SQLite's
            # write lock would make every other write wait on it, the
            # editors' rooms included.
            self.session.commit()
            if translate:
                await self._translate_missing(project_id, language)
        self.session.commit()

        return self.get_available_languages_optimized(project_id)

    def remove_project_language(
        self, project_id: UUID4, language: LanguageCode
    ) -> list[ProjectLanguage]:
        """Stop offering `language`, and delete its text.

        Refuses to remove the default language or the last remaining one:
        either would leave the project without a fallback language, which the
        interview and translation paths assume exists.
        """
        content = self._content(project_id)
        self._require_language(content, language)

        if len(content.languages) <= 1:
            raise ProjectLanguageError("A project must keep at least one language.")

        if language == content.default_language:
            raise ProjectLanguageError(
                f"{get_language_dict(language)['name']} is the project's default "
                "language. Pick a different default before removing it."
            )

        # In one write with the list, so no text outlives its language: kept,
        # it would come back as a stale translation if the language were
        # re-added.
        content.languages = [lang for lang in content.languages if lang != language]
        content.languages_in_progress = [
            lang for lang in content.languages_in_progress if lang != language
        ]
        content.guide = _without_language(content.guide, language)
        if content.consent is not None:
            content.consent = _without_language(content.consent, language)
        if content.welcome is not None:
            content.welcome = _without_language(content.welcome, language)
        content.emails = _without_language(content.emails, language)
        self.session.commit()

        return self.get_available_languages_optimized(project_id)

    def set_default_language(
        self, project_id: UUID4, language: LanguageCode
    ) -> list[ProjectLanguage]:
        """Make `language`, which the project must offer, its default."""
        content = self._content(project_id)
        if language not in content.languages:
            raise ProjectLanguageError(
                f"The project has no {get_language_dict(language)['name']} "
                "localization, so it cannot be the default. Add the language first."
            )

        if language in content.languages_in_progress:
            raise ProjectLanguageError(
                f"{get_language_dict(language)['name']} is still in progress. "
                "Mark it ready before making it the default."
            )

        content.default_language = language
        self.session.commit()

        return self.get_available_languages_optimized(project_id)

    def set_language_in_progress(
        self, project_id: UUID4, language: LanguageCode, in_progress: bool
    ) -> list[ProjectLanguage]:
        """Mark a language the project offers as still being translated, or
        as ready.

        In progress, it stays in the draft -- editors and test interviews use
        it -- but publishing neither offers it to respondents nor requires it to
        be complete. Refuses the default language, which every other one falls
        back to.
        """
        content = self._content(project_id)
        self._require_language(content, language)
        if in_progress and language == content.default_language:
            raise ProjectLanguageError(
                f"{get_language_dict(language)['name']} is the project's default "
                "language, which respondents always get. Pick a different "
                "default first."
            )

        rest = [lang for lang in content.languages_in_progress if lang != language]
        content.languages_in_progress = [*rest, language] if in_progress else rest
        self.session.commit()

        return self.get_available_languages_optimized(project_id)

    async def translate_missing(
        self, project_id: UUID4, language: LanguageCode
    ) -> ProjectContentTable:
        """Translate whatever text the guide, consent and welcome lack in
        `language` from the default language, leaving what it has alone.

        Not the participant emails: they are HTML with Jinja placeholders,
        which the string translator does not preserve.
        """
        self._require_language(self._content(project_id), language)
        await self._translate_missing(project_id, language)
        return self._content(project_id)

    async def _translate_missing(self, project_id: UUID4, language: LanguageCode):
        """Translate the guide, consent and welcome as they stand, then merge
        the translation into them as they stand *after*, and commit.

        The LLM call takes long enough for editors to save meanwhile (their
        rooms write these columns every couple of seconds), and writing the
        translated copies whole would undo whatever they saved. Where an
        editor changed the same text in the meantime, the editor's is kept.
        """
        content = self._content(project_id)
        # This session may have loaded the row before a room last saved it.
        self.session.refresh(content)
        # Dumped now: the translator may change what it is given in place.
        before = {name: _dump(getattr(content, name)) for name in _TRANSLATED}
        kwargs = {
            "target": language,
            "source": content.default_language,
            "model": TRANSLATION_MODEL,
        }

        async def maybe[T: (LocalizedConsent, LocalizedWelcome)](part: T | None):
            return None if part is None else await translate_missing(part, **kwargs)

        translated = await asyncio.gather(
            translate_missing(content.guide, **kwargs),
            maybe(content.consent),
            maybe(content.welcome),
        )
        self.session.refresh(content)
        for name, value in zip(_TRANSLATED, translated, strict=True):
            current = getattr(content, name)
            setattr(content, name, _merge_translation(before[name], current, value))
        self.session.commit()

    def get_default_language(self, project_id: UUID4) -> LanguageCode:
        """The project's default language, without loading its content."""
        statement = select(ProjectContentTable.default_language).where(
            ProjectContentTable.project_id == project_id
        )
        return self.session.execute(statement).scalar_one()

    def get_available_languages_optimized(
        self, project_id: uuid.UUID, source: ContentSource = "draft"
    ) -> list[ProjectLanguage]:
        """The languages the project offers, without loading its content when
        they are the draft's. Respondents get the published version's: a
        language added to the draft is not theirs until it is published."""
        if source != "draft":
            version = self._source(project_id, source)
            offered = version.offered if source == "published" else version.languages
            return _sorted_project_languages(
                offered, version.default_language, version.languages_in_progress
            )
        row = self.session.execute(
            select(
                ProjectContentTable.languages,
                ProjectContentTable.default_language,
                ProjectContentTable.languages_in_progress,
            ).where(ProjectContentTable.project_id == project_id)
        ).one()
        return _sorted_project_languages(
            row.languages, row.default_language, row.languages_in_progress
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

    def _content(
        self,
        project_id: UUID4,
        revision: int | None = None,
        part: ContentPart | None = None,
    ) -> ProjectContentTable:
        """The draft. With `revision`, for writing `part`: raises
        ContentRevisionError unless that part is still at it, so a save based
        on what someone else has since changed is refused, not applied."""
        statement = select(ProjectContentTable).where(
            ProjectContentTable.project_id == project_id
        )
        content = self.session.execute(statement).scalar_one()
        if revision is not None and (current := content.revision_of(part)) != revision:
            raise ContentRevisionError(revision, current, part)
        return content

    def get_revision(self, project_id: UUID4, part: ContentPart) -> int:
        """`part`'s revision, what `If-Match` must carry to write it."""
        return self.session.execute(
            select(getattr(ProjectContentTable, part.revision_column)).where(
                ProjectContentTable.project_id == project_id
            )
        ).scalar_one()

    def get_guide(self, project_id: UUID4) -> LocalizedInterviewGuide:
        return self._content(project_id).guide

    def update_guide(
        self,
        project_id: UUID4,
        guide: LocalizedInterviewGuide,
        revision: int | None = None,
    ):
        self._content(project_id, revision, ContentPart.GUIDE).guide = guide
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

    def get_external_param_values_for_interviews(
        self,
        project_id: UUID4,
        interview_ids: Sequence[UUID4],
    ) -> dict[UUID4, dict[str, Any]]:
        """External param values per interview, keyed by interview id.

        Interviews without stored values are omitted.
        """
        if not interview_ids:
            return {}

        statement = select(InterviewTable.id, InterviewTable.external_params).where(
            InterviewTable.project_id == project_id,
            InterviewTable.id.in_(interview_ids),
        )

        return {
            interview_id: values
            for interview_id, values in self.session.execute(statement).all()
            if values
        }

    def get_external_params(self, project_id: UUID4) -> list[ExternalParam]:
        """The project's declared external param schema (empty if unset)."""
        statement = select(ProjectTable.external_params).where(
            ProjectTable.id == project_id
        )

        return self.session.execute(statement).scalar_one() or []

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

    def get_agent_configs(
        self, project_id: UUID4, source: ContentSource = "draft"
    ) -> AgentConfigs:
        return self._source(project_id, source).agent_configs

    def update_agent_configs(
        self,
        project_id: UUID4,
        agent_configs: AgentConfigs,
        revision: int | None = None,
    ):
        self._content(
            project_id, revision, ContentPart.AGENTS
        ).agent_configs = agent_configs
        self.session.commit()

    # ==================== Consent / Welcome Methods ====================

    def get_consent(self, project_id: UUID4) -> LocalizedConsent | None:
        return self._content(project_id).consent

    def update_consent(
        self,
        project_id: UUID4,
        consent: LocalizedConsent | None,
        revision: int | None = None,
    ):
        self._content(project_id, revision, ContentPart.CONSENT).consent = consent
        self.session.commit()

    def get_welcome(self, project_id: UUID4) -> LocalizedWelcome | None:
        return self._content(project_id).welcome

    def update_welcome(
        self,
        project_id: UUID4,
        welcome: LocalizedWelcome | None,
        revision: int | None = None,
    ):
        self._content(project_id, revision, ContentPart.WELCOME).welcome = welcome
        self.session.commit()

    # ============ Participant Email Template Methods ============

    def _email(
        self, project_id: UUID4, reminder: bool, source: ContentSource = "draft"
    ) -> ParticipantEmail:
        emails = self._source(project_id, source).emails
        return emails.reminder if reminder else emails.invite

    def _set_email(
        self,
        project_id: UUID4,
        reminder: bool,
        email: ParticipantEmail,
        revision: int | None,
    ):
        content = self._content(project_id, revision, ContentPart.EMAILS)
        content.emails = content.emails.model_copy(
            update={"reminder" if reminder else "invite": email}
        )
        self.session.commit()

    def _email_in(
        self, project_id: UUID4, language: LanguageCode, reminder: bool
    ) -> tuple[str | None, str | None]:
        self._require_language(self._content(project_id), language)
        return self._email(project_id, reminder).get(language)

    def _emails_ordered(
        self, project_id: UUID4, reminder: bool, source: ContentSource
    ) -> list[tuple[LanguageCode, str | None, str | None]]:
        content = self._source(project_id, source)
        email = self._email(project_id, reminder, source)
        languages = sorted(
            # What is sent goes to languages respondents are offered.
            content.offered if source == "published" else content.languages,
            key=lambda language: (language != content.default_language, language),
        )
        return [(language, *email.get(language)) for language in languages]

    def get_participant_email_template(
        self, project_id: UUID4, language: LanguageCode
    ) -> tuple[str | None, str | None]:
        """Return (subject, template) of the invitation in `language`."""
        return self._email_in(project_id, language, reminder=False)

    def set_participant_email_template(
        self,
        project_id: UUID4,
        language: LanguageCode,
        subject: str | None,
        template: str | None,
        revision: int | None = None,
    ) -> None:
        email = self._email(project_id, reminder=False)
        self._set_email(
            project_id, False, email.set(language, subject, template), revision
        )

    def get_participant_email_templates_ordered(
        self,
        project_id: UUID4,
        source: ContentSource = "published",
    ) -> list[tuple[LanguageCode, str | None, str | None]]:
        """Return (language, subject, template) of the invitation for every
        language `source` offers, the default first. The published version's
        by default: this is what is sent."""
        return self._emails_ordered(project_id, reminder=False, source=source)

    # ====== Participant Reminder Email Template Methods ======

    def get_participant_reminder_email_template(
        self, project_id: UUID4, language: LanguageCode
    ) -> tuple[str | None, str | None]:
        """Return (subject, template) of the reminder email in `language`."""
        return self._email_in(project_id, language, reminder=True)

    def set_participant_reminder_email_template(
        self,
        project_id: UUID4,
        language: LanguageCode,
        subject: str | None,
        template: str | None,
        revision: int | None = None,
    ) -> None:
        email = self._email(project_id, reminder=True)
        self._set_email(
            project_id, True, email.set(language, subject, template), revision
        )

    def get_participant_reminder_email_templates_ordered(
        self,
        project_id: UUID4,
        source: ContentSource = "published",
    ) -> list[tuple[LanguageCode, str | None, str | None]]:
        """Return (language, subject, template) of the reminder email for every
        language `source` offers, the default first. The published version's
        by default: this is what is sent."""
        return self._emails_ordered(project_id, reminder=True, source=source)

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

    def can_moderate_project(
        self, user_id: UUID4, project_id: UUID4, scope: Scope
    ) -> bool:
        """Whether the user may act on other people's contributions here."""
        return can_moderate_project(self.session, user_id, project_id, scope)

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
