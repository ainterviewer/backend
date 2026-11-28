"""Users, folders and collaboration fixtures."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.crud import InterviewDataBase
from app.db.tables import CollaboratorTable, UserTable
from app.schemas.users import UserCreate
from app.types import CollaboratorRole, PreviewFeature, Scope

DEV_EMAIL = "dev@example.com"
DEV_PASSWORD = "dev-password"
DEMO_EMAIL = "demo@example.com"
DEMO_PASSWORD = "demo-password"


@dataclass(frozen=True)
class SeedAccounts:
    owner_id: UUID
    research_folder_id: UUID
    demo_folder_id: UUID
    demo_user_id: UUID
    demo_user_folder_id: UUID


def create(db: InterviewDataBase, session: Session) -> SeedAccounts:
    owner = db.users.create_user(
        UserCreate(
            email=DEV_EMAIL,
            password=DEV_PASSWORD,
            first_name="Dev",
            last_name="Researcher",
            invite_token=None,
            scope=Scope.ADMIN,
        )
    )
    owner_row = session.get_one(UserTable, owner.id)
    owner_row.email_verified = True
    owner_row.preview_features = list(PreviewFeature)
    owner_row.two_factor_enabled = False
    session.commit()
    collaborator = db.users.create_user(
        UserCreate(
            email="collaborator@example.com",
            password="collaborator-password",
            first_name="Casey",
            last_name="Collaborator",
            invite_token=None,
        )
    )
    collaborator_row = session.get_one(UserTable, collaborator.id)
    collaborator_row.email_verified = True
    collaborator_row.two_factor_enabled = False
    session.commit()
    demo_user = db.users.create_user(
        UserCreate(
            email=DEMO_EMAIL,
            password=DEMO_PASSWORD,
            first_name="Dana",
            last_name="Demo",
            invite_token=None,
            scope=Scope.DEMO,
        )
    )
    demo_user_row = session.get_one(UserTable, demo_user.id)
    demo_user_row.email_verified = True
    demo_user_row.two_factor_enabled = False
    session.commit()
    research = db.projects.create_folder("Research studies", owner.id)
    demos = db.projects.create_folder("Guide feature demos", owner.id)
    # Registration gives every account a personal folder to start from. Made
    # last so the other folders keep their seeded IDs.
    demo_user_folder = db.projects.create_folder("Dana's Personal Folder", demo_user.id)
    session.add(
        CollaboratorTable(
            folder_id=research.id,
            user_id=collaborator.id,
            role=CollaboratorRole.EDITOR,
            added_by_id=owner.id,
        )
    )
    session.commit()
    return SeedAccounts(
        owner.id, research.id, demos.id, demo_user.id, demo_user_folder.id
    )
