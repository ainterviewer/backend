from fastapi import APIRouter, UploadFile

from pydantic import UUID4

from ...db.models import (
    ParticipantCreate,
    ParticipantPublic,
    ParticipantUpdate,
)
from ...dependencies import (
    DBSession,
    UserToken,
    ProjectAdmin,
    ProjectEditor,
    ProjectViewer,
)
from ..request_models import DeleteParticipantsRequest, LinkParticipantRequest

router = APIRouter(tags=["participants"])


@router.get("/projects/{project_id}/participants")
async def get_participants(
    project_id: UUID4,
    db: DBSession,
    jwt: UserToken,
    _: ProjectViewer,
) -> list[ParticipantPublic]:
    return db.participants.get_participants(project_id)


@router.get("/projects/{project_id}/participants/{participant_id}")
async def get_participant(
    project_id: UUID4,
    participant_id: UUID4,
    db: DBSession,
    jwt: UserToken,
    _: ProjectViewer,
) -> ParticipantPublic:
    return db.participants.get_participant(participant_id)


@router.post("/projects/{project_id}/participants/{participant_id}")
async def opt_out(
    project_id: UUID4,
    participant_id: UUID4,
    db: DBSession,
) -> ParticipantPublic:
    return db.participants.update_participant(
        participant_id, ParticipantUpdate(participating=False)
    )


@router.post("/projects/{project_id}/participants")
async def add_participant(
    project_id: UUID4,
    participant: ParticipantCreate,
    db: DBSession,
    jwt: UserToken,
    _: ProjectEditor,
) -> ParticipantPublic:
    return db.participants.add_participant(project_id, participant)


@router.post("/projects/{project_id}/participants/bulk")
async def add_participants(
    project_id: UUID4,
    participants: list[ParticipantCreate],
    db: DBSession,
    jwt: UserToken,
    _: ProjectEditor,
) -> list[ParticipantPublic]:
    return db.participants.add_participants(project_id, participants)


@router.post("/projects/{project_id}/participants/upload")
async def upload_participants(
    project_id: UUID4,
    file: UploadFile,
    db: DBSession,
    jwt: UserToken,
    _: ProjectEditor,
) -> list[ParticipantPublic]:
    content = await file.read()
    return db.participants.add_participants_from_csv(project_id, content)


@router.patch("/projects/{project_id}/participants/{participant_id}")
async def update_participant(
    project_id: UUID4,
    participant_id: UUID4,
    update: ParticipantUpdate,
    db: DBSession,
    jwt: UserToken,
    _: ProjectEditor,
) -> ParticipantPublic:
    return db.participants.update_participant(participant_id, update)


@router.delete("/projects/{project_id}/participants/{participant_id}")
async def delete_participant(
    project_id: UUID4,
    participant_id: UUID4,
    db: DBSession,
    jwt: UserToken,
    _: ProjectAdmin,
):
    db.participants.remove_participant(participant_id)


@router.delete("/projects/{project_id}/participants")
async def delete_participants(
    project_id: UUID4,
    delete_request: DeleteParticipantsRequest,
    db: DBSession,
    jwt: UserToken,
    _: ProjectAdmin,
):
    db.participants.remove_participants(project_id, delete_request.participant_ids)


@router.put("/projects/{project_id}/interviews/{interview_id}/participant")
async def link_participant_to_interview(
    project_id: UUID4,
    interview_id: UUID4,
    link_request: LinkParticipantRequest,
    db: DBSession,
    jwt: UserToken,
    _: ProjectEditor,
):
    db.participants.link_to_interview(interview_id, link_request.participant_id)
