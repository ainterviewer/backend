import logging

from fastapi import APIRouter, BackgroundTasks, Request
from pydantic import UUID4

from ainterviewer.settings import settings as lib_settings

from ...auth import AuthToken
from ...db.repositories.errors import AlreadyCollaboratorError
from ...dependencies import (
    DBSession,
    DemoToken,
    FolderAdmin,
    FolderEditor,
    FolderViewer,
)
from ...schemas.projects import (
    ArchiveRequest,
    CollaboratorInviteCreate,
    CollaboratorInviteDetails,
    CollaboratorInvitePublic,
    CollaboratorInviteRequest,
    CollaboratorPublic,
    FolderCreate,
    FolderPublic,
    FolderUpdate,
    FolderWithProjectsPublic,
    IssuedCollaboratorInvite,
    ProjectCreate,
    ProjectCreateResponse,
)
from ...schemas.users import UserPublic
from ...services.email.mail import email_templates, send_email
from ...settings import app_settings
from ...types import CollaboratorRole, Scope
from ...utils import generate_qr_img
from ..auth import EmailSendError
from ..websockets.manager import session_manager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["folders"])


@router.get(
    "/folders",
    description="Get project folders",
    response_model=list[FolderWithProjectsPublic],
)
async def get_folders(
    db: DBSession,
    jwt: DemoToken,
):
    return db.projects.get_folders(jwt.user_id, with_projects=True)


@router.post("/folders", description="Create new project folder")
async def create_folder(
    project_folder: FolderCreate,
    db: DBSession,
    jwt: DemoToken,
) -> FolderPublic:
    """Create a folder with the caller as admin, and invite the listed
    collaborators. An invite whose email fails to send is dropped, as it
    would be from the collaborators dialog; re-inviting sends a fresh one."""
    folder = db.projects.create_folder(project_folder.title, jwt.user_id)
    inviter = db.users.get_user_by_id(jwt.user_id)

    invited: set[str] = set()
    for collaborator in project_folder.collaborators:
        if collaborator.email.lower() in invited:
            continue
        invited.add(collaborator.email.lower())
        try:
            issued = db.projects.create_collaborator_invite(
                folder.id,
                collaborator.email,
                collaborator.role,
                jwt.user_id,
                actor_role=CollaboratorRole.ADMIN,  # the creator
            )
        except AlreadyCollaboratorError:  # the creator listed themselves
            continue
        try:
            await _send_invite_email(issued, inviter)
        except Exception:
            logger.exception("Failed to send collaborator invite %s", issued.invite.id)
            db.projects.delete_collaborator_invite(
                folder.id, issued.invite.id, CollaboratorRole.ADMIN
            )

    return folder


@router.delete("/folders/{folder_id}", description="Delete folder")
async def delete_folder(
    folder_id: UUID4,
    db: DBSession,
    jwt: DemoToken,
    _: FolderAdmin,
):
    await session_manager.end(db.projects.get_folder_project_ids(folder_id))
    db.projects.delete_folder(folder_id)


@router.patch("/folders/{folder_id}", description="Delete folder")
async def edit_folder(
    folder_id: UUID4,
    project_folder: FolderUpdate,
    db: DBSession,
    jwt: DemoToken,
    _: FolderAdmin,
):
    db.projects.update_folder(folder_id, project_folder.title)


@router.patch(
    "/folders/{folder_id}/archive", description="Archive or unarchive a folder"
)
async def set_folder_archived(
    folder_id: UUID4,
    archive_request: ArchiveRequest,
    db: DBSession,
    jwt: DemoToken,
    _: FolderAdmin,
):
    db.projects.set_folder_archived(folder_id, archive_request.archived)


@router.get(
    "/folders/{folder_id}/collaborators", response_model=list[CollaboratorPublic]
)
async def get_collaborators(
    request: Request,
    folder_id: UUID4,
    db: DBSession,
    jwt: DemoToken,
    _: FolderEditor,
):
    return db.projects.get_collaborators(folder_id)


def _role_on_folder(
    db: DBSession, jwt: AuthToken, folder_id: UUID4
) -> CollaboratorRole:
    """The caller's role on the folder, which bounds the collaborator changes
    they may make. A platform admin passes every role check, so counts as an
    admin here too. Only called behind a folder role check, so there is one."""
    if Scope.ADMIN in {Scope(scope) for scope in jwt.scope.split()}:
        return CollaboratorRole.ADMIN
    role = db.projects.get_user_role_on_folder(jwt.user_id, folder_id)
    assert role is not None
    return role


def _role_with_article(role: CollaboratorRole) -> tuple[str, str]:
    return ("an" if role[0] in "aeiou" else "a"), role.value


async def _send_invite_email(
    issued: IssuedCollaboratorInvite, inviter: UserPublic
) -> None:
    """Email an invite's link. Someone without an account is pointed at the
    access request form first; the link still works once they have one."""
    public_url = app_settings.app.public_url
    inviter_name = " ".join(filter(None, [inviter.first_name, inviter.last_name]))
    article, role = _role_with_article(issued.invite.role)
    expires_at = issued.invite.expires_at
    await send_email(
        issued.invite.email,
        f"{inviter_name} invited you to collaborate on AInterviewer",
        html_content=email_templates.get_template("collaborator_invite.jinja").render(
            recipient_name=issued.recipient_first_name or "there",
            inviter_name=inviter_name,
            folder_title=issued.folder_title,
            role_article=article,
            role=role,
            has_account=issued.has_account,
            accept_link=f"{public_url}/dashboard/invites/accept?token={issued.token}",
            request_access_link=f"{public_url}/login?request",
            expires_on=f"{expires_at.day} {expires_at:%B %Y}",
        ),
    )


@router.get("/folders/{folder_id}/invites")
async def get_collaborator_invites(
    folder_id: UUID4,
    db: DBSession,
    jwt: DemoToken,
    _: FolderEditor,
) -> list[CollaboratorInvitePublic]:
    """The folder's pending invites, expired ones included."""
    return db.projects.get_collaborator_invites(folder_id)


@router.post("/folders/{folder_id}/invites")
async def create_collaborator_invite(
    folder_id: UUID4,
    body: CollaboratorInviteCreate,
    db: DBSession,
    jwt: DemoToken,
    _: FolderEditor,
) -> CollaboratorInvitePublic:
    """Invite someone by email, or resend their pending invite with this role.

    Answers the same whether or not the email has an account, and the invite
    is listed as pending either way until its recipient accepts, so the form
    cannot be used to find out who is a user. 409 `already_collaborator` if
    they already collaborate on the folder, 403 `role_above_own` for a role
    above the caller's.
    """
    issued = db.projects.create_collaborator_invite(
        folder_id,
        body.email,
        body.role,
        jwt.user_id,
        actor_role=_role_on_folder(db, jwt, folder_id),
    )
    try:
        await _send_invite_email(issued, db.users.get_user_by_id(jwt.user_id))
    except Exception:
        logger.exception("Failed to send collaborator invite %s", issued.invite.id)
        # Cleanup of what this request made, not a revocation: no ceiling.
        db.projects.delete_collaborator_invite(
            folder_id, issued.invite.id, CollaboratorRole.ADMIN
        )
        raise EmailSendError from None
    return issued.invite


@router.delete("/folders/{folder_id}/invites/{invite_id}")
async def delete_collaborator_invite(
    folder_id: UUID4,
    invite_id: UUID4,
    db: DBSession,
    jwt: DemoToken,
    _: FolderEditor,
):
    """Revoke a pending invite; its link stops working. 403 `role_above_own`
    for an invite above the caller's role."""
    db.projects.delete_collaborator_invite(
        folder_id, invite_id, _role_on_folder(db, jwt, folder_id)
    )


@router.post("/collaborator-invites/inspect")
async def inspect_collaborator_invite(
    body: CollaboratorInviteRequest, db: DBSession, jwt: DemoToken
) -> CollaboratorInviteDetails:
    """What an invite link offers, for its recipient to accept or decline.
    404 `invite_invalid`, 410 `invite_expired`."""
    return db.projects.get_collaborator_invite_details(body.token)


@router.post("/collaborator-invites/accept")
async def accept_collaborator_invite(
    body: CollaboratorInviteRequest, db: DBSession, jwt: DemoToken
) -> FolderPublic:
    """Join the folder with the invite's role. The link is the proof, so any
    signed-in account holding it may accept."""
    return db.projects.accept_collaborator_invite(body.token, jwt.user_id)


@router.post("/collaborator-invites/decline")
async def decline_collaborator_invite(
    body: CollaboratorInviteRequest, db: DBSession, jwt: DemoToken
):
    db.projects.decline_collaborator_invite(body.token)


@router.delete("/folders/{folder_id}/collaborators")
async def remove_collaborator(
    folder_id: UUID4,
    user_id: UUID4,
    db: DBSession,
    jwt: DemoToken,
    _: FolderEditor,
):
    """403 `role_above_own` for someone above the caller's role, 409
    `last_admin` for the folder's only admin."""
    db.projects.remove_collaborator(
        folder_id, user_id, _role_on_folder(db, jwt, folder_id)
    )


@router.post("/folders/{folder_id}/leave")
async def leave_folder(
    folder_id: UUID4,
    db: DBSession,
    jwt: DemoToken,
    _: FolderViewer,
):
    """Stop collaborating on the folder. Any role may; 409 `last_admin` for
    its only admin, who has to make someone else admin first."""
    db.projects.leave_folder(folder_id, jwt.user_id)


@router.patch("/folders/{folder_id}/collaborators", response_model=CollaboratorPublic)
async def update_collaborator_role(
    folder_id: UUID4,
    user_id: UUID4,
    role: CollaboratorRole,
    db: DBSession,
    jwt: DemoToken,
    _: FolderEditor,
):
    """Neither the collaborator's current role nor the new one may be above
    the caller's (403 `role_above_own`); 409 `last_admin` for demoting the
    folder's only admin."""
    return db.projects.update_collaborator_role(
        folder_id, user_id, role, _role_on_folder(db, jwt, folder_id)
    )


@router.post("/folders/{folder_id}/projects", description="Create a new project")
async def create_project(
    request: Request,
    folder_id: UUID4,
    project_request: ProjectCreate,
    background_tasks: BackgroundTasks,
    db: DBSession,
    jwt: DemoToken,
    _: FolderEditor,
) -> ProjectCreateResponse:
    project_id = db.projects.create_project(
        folder_id=folder_id,
        title=project_request.title,
        owner_id=jwt.user_id,
        default_language=project_request.default_language,
    )

    file_path = (
        lib_settings.storage.project_storage.qr_code_path(project_id) / "interview.png"
    )
    interview_url = str(request.base_url) + f"interview?id={project_id}"

    background_tasks.add_task(generate_qr_img, str(interview_url), file_path=file_path)

    return ProjectCreateResponse(project_id=project_id)
