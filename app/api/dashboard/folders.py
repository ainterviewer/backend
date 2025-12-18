from fastapi import APIRouter

from ...db.models import (
    ProjectFolderCreate,
    ProjectFolderDelete,
    ProjectFolderEdit,
    ProjectFolderPublic,
    ProjectFolderWithProjects,
)
from ...dependencies import DBSession, UserToken

router = APIRouter()


@router.get(
    "/folders",
    description="Get project folders",
    response_model=list[ProjectFolderWithProjects],
)
async def get_folders(db: DBSession, jwt: UserToken):
    return db.projects.get_folders(jwt.user_id, with_projects=True)


@router.post("/folders", description="Create new project folder")
async def create_folder(
    project_folder: ProjectFolderCreate,
    db: DBSession,
    jwt: UserToken,
) -> ProjectFolderPublic:
    return db.projects.create_folder(project_folder.title, jwt.user_id)


@router.delete("/folders", description="Delete folder")
async def delete_folder(
    project_folder: ProjectFolderDelete,
    db: DBSession,
    jwt: UserToken,
):
    db.projects.delete_folder(project_folder.id)


@router.patch("/folders", description="Delete folder")
async def edit_folder(
    project_folder: ProjectFolderEdit,
    db: DBSession,
    jwt: UserToken,
):
    db.projects.update_folder(project_folder.id, project_folder.title)
