"""Who may moderate a project.

Editing or deleting somebody else's contribution -- a comment today -- takes
more than access to the project: it takes platform admin, ownership of the
project, or the ADMIN role on its folder.

The rule lives here rather than inside one repository because two kinds of
caller need it: the endpoints that enforce it, and the one that answers "may
I?" so the UI can hide the buttons the API would refuse. A second copy of the
rule is a second chance for them to drift apart.
"""

from pydantic import UUID4
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...types import CollaboratorRole, Scope
from ..tables import CollaboratorTable, ProjectFolderTable, ProjectTable


def can_moderate_project(
    session: Session, user_id: UUID4, project_id: UUID4, scope: Scope
) -> bool:
    """Whether `user_id` may act on other people's contributions in a project.

    False for a project that does not exist, so a caller naming an unknown id
    is refused rather than let through.
    """
    if scope == Scope.ADMIN:
        return True

    statement = (
        select(ProjectTable.owner_id, CollaboratorTable.role)
        .select_from(ProjectTable)
        .join(ProjectFolderTable, ProjectFolderTable.id == ProjectTable.folder_id)
        .outerjoin(
            CollaboratorTable,
            (CollaboratorTable.folder_id == ProjectFolderTable.id)
            & (CollaboratorTable.user_id == user_id),
        )
        .where(ProjectTable.id == project_id)
    )
    row = session.execute(statement).first()
    if row is None:
        return False

    owner_id, role = row
    return owner_id == user_id or (
        role is not None and role.includes(CollaboratorRole.ADMIN)
    )
