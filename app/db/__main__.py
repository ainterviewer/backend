import json
from pathlib import Path
from uuid import UUID

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ainterviewer.agents.config import read_agent_configs
from ainterviewer.agents.prompts.models import DEFAULT_PROMPTS
from ainterviewer.config import read_configs, read_interview_config
from ainterviewer.interview_guides import InterviewGuide
from ainterviewer.types import DatabaseType

from ..settings import app_settings
from .crud import InterviewDataBase
from .models import UserCreate
from .vectors import register_vector_extension


def parse_args():
    import argparse

    parser = argparse.ArgumentParser(description="create database")

    parser.add_argument("--database-file", type=str, help="database uri")
    parser.add_argument(
        "--recreate-db",
        action="store_true",
        help="delete and recreate the db and tables",
    )
    parser.add_argument(
        "--setup-db",
        action="store_true",
        help="setup the database with initial data",
    )
    parser.add_argument(
        "--update-models",
        action="store_true",
        help="update models",
    )
    parser.add_argument(
        "--create-project",
        action="store_true",
        help="create a new project",
    )
    parser.add_argument(
        "--update-projects",
        action="store_true",
        help="Update existing projects from the data/projects directory",
    )
    parser.add_argument(
        "--update-interview-guide",
        action="store_true",
        help="update the interview guide",
    )
    parser.add_argument(
        "--update-interview-config",
        action="store_true",
        help="update the interview config",
    )
    parser.add_argument(
        "--update-prompts",
        action="store_true",
        help="update the prompts",
    )
    parser.add_argument(
        "--interview-title",
        help="title of the interview",
    )
    parser.add_argument(
        "--interview-guide-path",
        type=str,
        help="interview guide path",
    )
    parser.add_argument(
        "--interview-config-path",
        type=str,
        help="Interview config path",
    )
    parser.add_argument(
        "--create-interviews",
        action="store_true",
        help="create a interview",
    )
    parser.add_argument(
        "--create-users",
        action="store_true",
        help="create users",
    )
    parser.add_argument(
        "--users-file",
        default="storage/users/default.json",
        help="users file",
        type=Path,
    )
    parser.add_argument(
        "--n-interviews",
        "-n",
        type=int,
        help="number of interviews to create",
    )
    parser.add_argument(
        "--project-id",
        type=UUID,
        help="project id",
    )
    parser.add_argument(
        "--create-invite",
        action="store_true",
        help="create an invitation",
    )

    args = parser.parse_args()

    if args.create_interviews and args.n_interviews is None:
        parser.error("--n-interviews is required when --create-interviews is used")

    if args.update_interview_guide and args.interview_guide_path is None:
        parser.error(
            "--interview-guide-path is required when --create-project or --update-interview-guide is used"
        )
    if args.update_interview_config and args.interview_config_path is None:
        parser.error(
            "--interview-config-path is required when --update-interview-config is used"
        )

    if args.create_project and args.interview_config_path is None:
        parser.error("--models-config is required when --create-project is used")
    if args.create_project and args.interview_title is None:
        parser.error("--interview-title is required when --create-project is used")

    if (
        args.update_interview_guide
        or args.update_interview_config
        or args.create_interviews
    ) and args.project_id is None:
        parser.error("You must supply --project-id for this task.")

    eval = [
        not args.recreate_db,
        not args.update_models,
        not args.create_users,
        not args.create_project,
        not args.setup_db,
        not args.update_interview_guide,
        not args.update_interview_config,
        not args.update_prompts,
        not args.create_interviews,
        not args.create_invite,
        not args.update_projects,
    ]

    if args.setup_db:
        args.recreate_db = True
        args.create_users = True

    if all(eval):
        parser.error(
            "At least one of the setup or update commands (--recreate-db, --setup-db, --update-projects, etc.) must be used."
        )

    return args


if __name__ == "__main__":
    args = parse_args()

    engine = create_engine(app_settings.database.connection_string, echo=True)
    register_vector_extension(engine)
    print(f"Connecting to database at {app_settings.database.connection_string}")

    session = Session(engine)
    db = InterviewDataBase(session)

    if args.update_models:
        db.create_db_and_tables()
        print("Models updated")

    if args.recreate_db:
        DB_FILE = Path(
            f"{app_settings.database.db_path}/{app_settings.database.database_file}"
        )
        if DB_FILE.exists():
            while (
                answer := input(
                    "Are you sure you want to delete the database and all data? y/n\n"
                ).lower()
            ) not in ["y", "n"]:
                print("Invalid input")
            if answer == "y":
                if app_settings.database.db == DatabaseType.SQLITE:
                    DB_FILE.unlink(missing_ok=True)
            else:
                print("Abort. Database not deleted.")
                exit()

        db.create_db_and_tables()
        print("Database and tables recreated")

    if args.setup_db:
        raise NotImplementedError
        for project in Path.cwd().glob("data/projects/*"):
            project_title = project.name.replace("_", " ").title()

            if (config_file := project / "config.yaml").exists():
                interview_config = read_interview_config(config_file)
            else:
                interview_config = None

            if (id_file := project / "id").exists():
                project_id = UUID(id_file.read_text().strip())
            else:
                project_id = None

            project_id = db.projects.create_project(
                title=project_title,
                interview_config=interview_config,
                project_id=project_id,
            )

            for lang in project.glob("localizations/*"):
                lang_code = lang.name.upper()
                db.add_project_language(project_id, lang_code)

                if (path := lang / "interview_guide.json").exists():
                    with path.open() as f:
                        interview_guide = InterviewGuide.model_validate(json.load(f))

                    db.update_interview_guide(
                        project_id=project_id,
                        interview_guide_content=interview_guide,
                        language=lang_code,
                    )

                if (agents_config_file := lang / "agents.yaml").exists():
                    agents_config = read_agent_configs(agents_config_file)
                    db.update_agent_configs(project_id, None, lang_code, agents_config)

                # Update prompts
                prompts = DEFAULT_PROMPTS.model_copy(deep=True)

                if prompt_files := list((lang / "prompts").glob("*.jinja")):
                    for prompt_file in prompt_files:
                        with open(prompt_file, "r") as f:
                            prompt = f.read()

                        _parts = prompt_file.stem.split("_")
                        agent = getattr(prompts, f"{_parts[0]}_{_parts[1]}")
                        setattr(agent, f"{_parts[2]}_{_parts[3]}", prompt)

                    db.set_prompts(
                        project_id,
                        team_id=None,
                        language=lang_code,
                        prompts=prompts,
                    )
                else:
                    db.set_prompts(
                        project_id,
                        team_id=None,
                        language=lang_code,
                        prompts=prompts,
                    )

    if args.update_projects:
        print("Updating projects from data/projects directory...")

        for project in Path.cwd().glob("data/projects/*"):
            project_title = project.name.replace("_", " ").title()

            # Read project ID from id file
            if (id_file := project / "id").exists():
                project_id = UUID(id_file.read_text().strip())
            else:
                print(
                    f"WARNING: No id file found for project '{project_title}'. Skipping update."
                )
                continue

            print(f"Found project '{project_title}'. Updating... (ID: {project_id})")
            updated = False

            # Update interview config if the file exists
            if (config_file := project / "config.yaml").exists():
                interview_config = read_interview_config(config_file)
                db.projects.update_interview_config(project_id, interview_config)
                print(f"  - Interview config updated for project {project_id}")
                updated = True

            # Update localized content
            for lang in project.glob("localizations/*"):
                lang_code = lang.name.upper()

                # Update interview guide if it exists
                if (path := lang / "interview_guide.json").exists():
                    with path.open() as f:
                        interview_guide = json.load(f)

                    interview_guide = InterviewGuide(**interview_guide)
                    print(
                        f"  - Updating interview guide for {lang_code} in project {project_id}"
                    )

                    db.update_interview_guide(
                        project_id=project_id,
                        interview_guide_content=interview_guide,
                        language=lang_code,
                    )
                    updated = True

                # Update agent configs if they exist
                if (agents_config_file := lang / "agents.yaml").exists():
                    agents_config = read_agent_configs(agents_config_file)
                    db.projects.update_agent_configs(
                        project_id, lang_code, agents_config
                    )
                    print(
                        f"  - Agent configs updated for {lang_code} in project {project_id}"
                    )
                    updated = True

                # Update prompts
                prompts = DEFAULT_PROMPTS.model_copy(deep=True)

                if prompt_files := list((lang / "prompts").glob("*.jinja")):
                    for prompt_file in prompt_files:
                        with open(prompt_file, "r") as f:
                            prompt = f.read()

                        _parts = prompt_file.stem.split("_")
                        agent = getattr(prompts, f"{_parts[0]}_{_parts[1]}")
                        setattr(agent, f"{_parts[2]}_{_parts[3]}", prompt)

                    db.projects.set_prompts(
                        project_id,
                        language=lang_code,
                        prompts=prompts,
                    )
                else:
                    db.projects.set_prompts(
                        project_id,
                        language=lang_code,
                        prompts=prompts,
                    )
                print(f"  - Prompts updated for {lang_code} in project {project_id}")

            if not updated:
                print(f"  - No update files found for '{project_title}'.")

        print("Finished updating projects.")

    if args.create_users:
        file = args.users_file
        if not file.exists():
            print(f"{args.users_file} file not found")
            exit()

        with file.open() as f:
            users = json.load(f)

        n = 0
        for user in users:
            try:
                db.users.create_user(UserCreate(**user))
                n += 1
            except IntegrityError:
                print(f"User {user['email']} already exists")

        print(f"created {n} user{'s' if n > 1 else ''}")

    if args.create_project:
        raise NotImplementedError
        with open(args.interview_guide_path) as f:
            interview_guide = InterviewGuide(**json.load(f))

        interview_config, agents_config = read_configs(args.interview_config_path)

        project_id = db.create_project(
            title=args.interview_title,
            interview_config=interview_config,
            interview_guide_content=interview_guide,
            agent_configs=agents_config,
        )
        print(f"Project created with id {project_id}")

    if args.update_interview_guide:
        with open(args.interview_guide_path) as f:
            interview_guide = InterviewGuide(**json.load(f))

        db.update_interview_guide(args.project_id, interview_guide)
        print(f"Interview guide updated for project {args.project_id}")

    if args.update_interview_config:
        raise NotImplementedError
        models_config = read_configs(args.interview_config_path)
        db.update_interview_config(args.project_id, models_config)
        print(f"Interview config updated for project {args.project_id}")

    if args.update_prompts:
        for user in db.users.get_users():
            for folder in db.projects.get_folders(user_id=user.id):
                for project in db.projects.get_projects(
                    folder_id=folder.id, include_available_languages=True
                ):
                    for language in project.available_languages:  # ty:ignore[not-iterable]
                        db.projects.set_prompts(
                            project.id,
                            language=language["code"],
                            prompts=DEFAULT_PROMPTS,
                        )

    if args.create_interviews:
        for i in range(args.n_interviews):
            interview = db.create_interview(args.project_id)
            print(f"Interview created with id {interview.id}")

    if args.create_invite:
        raise NotImplementedError
        invite = db.create_invitation()
        print(f"Invite created with token {invite.token}")
        print(f"Invite link: {invite.invitation_link}")
