from fastapi import APIRouter, BackgroundTasks, Request
from pydantic import UUID4

from ainterviewer.config import InterviewConfig
from ainterviewer.settings import settings as lib_settings

from ...db.models import (
    CollaboratorCreate,
    CollaboratorPublic,
    ProjectFolderCreate,
    ProjectFolderEdit,
    ProjectFolderPublic,
    ProjectFolderWithProjects,
)
from ...dependencies import DBSession, FolderAdmin, FolderEditor, UserToken
from ...types import CollaboratorRole
from ...utils import generate_qr_img
from ..request_models import CreateProjectRequest

router = APIRouter(tags=["folders"])


@router.get(
    "/folders",
    description="Get project folders",
    response_model=list[ProjectFolderWithProjects],
)
async def get_folders(
    db: DBSession,
    jwt: UserToken,
):
    return db.projects.get_folders(jwt.user_id, with_projects=True)


@router.post("/folders", description="Create new project folder")
async def create_folder(
    project_folder: ProjectFolderCreate,
    db: DBSession,
    jwt: UserToken,
) -> ProjectFolderPublic:
    return db.projects.create_folder(
        project_folder.title, jwt.user_id, project_folder.collaborators
    )


@router.delete("/folders/{folder_id}", description="Delete folder")
async def delete_folder(
    folder_id: UUID4,
    db: DBSession,
    jwt: FolderAdmin,
):
    db.projects.delete_folder(folder_id)


@router.patch("/folders/{folder_id}", description="Delete folder")
async def edit_folder(
    folder_id: UUID4,
    project_folder: ProjectFolderEdit,
    db: DBSession,
    jwt: FolderAdmin,
):
    db.projects.update_folder(folder_id, project_folder.title)


@router.get(
    "/folders/{folder_id}/collaborators", response_model=list[CollaboratorPublic]
)
async def get_collaborators(
    request: Request,
    folder_id: UUID4,
    db: DBSession,
    jwt: FolderEditor,
):
    return db.projects.get_collaborators(folder_id)


@router.post("/folders/{folder_id}/collaborators", response_model=CollaboratorPublic)
async def add_collaborator(
    folder_id: UUID4,
    collaborator: CollaboratorCreate,
    db: DBSession,
    jwt: FolderEditor,
):
    return db.projects.add_collaborator(
        folder_id,
        collaborator.email,
        collaborator.role,
        jwt.user_id,
    )


@router.delete("/folders/{folder_id}/collaborators")
async def remove_collaborator(
    folder_id: UUID4,
    user_id: UUID4,
    db: DBSession,
    jwt: FolderEditor,
):
    db.projects.remove_collaborator(folder_id, user_id)


@router.patch("/folders/{folder_id}/collaborators", response_model=CollaboratorPublic)
async def update_collaborator_role(
    folder_id: UUID4,
    user_id: UUID4,
    role: CollaboratorRole,
    db: DBSession,
    jwt: FolderEditor,
):
    return db.projects.update_collaborator_role(folder_id, user_id, role)


@router.post("/folders/{folder_id}/projects", description="Create a new project")
async def create_project(
    request: Request,
    folder_id: UUID4,
    project_request: CreateProjectRequest,
    background_tasks: BackgroundTasks,
    db: DBSession,
    jwt: FolderEditor,
):
    project_id = db.projects.create_project(
        folder_id=folder_id,
        title=project_request.title,
        interview_config=InterviewConfig(
            default_language=project_request.default_language
        ),
    )

    file_path = (
        lib_settings.storage.project_storage.qr_code_path(project_id) / "interview.png"
    )
    interview_url = str(request.base_url) + f"interview?id={project_id}"

    background_tasks.add_task(generate_qr_img, str(interview_url), file_path)

    return {"project_id": project_id}
