"""Project, participant, test and experiment fixtures."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from ainterviewer.interview_guides import (
    InterviewGuide,
    LocalizedConsent,
    LocalizedInterviewGuide,
    LocalizedWelcome,
    Question,
    QuestionSection,
    RadioItem,
)
from ainterviewer.interview_guides.extra import Consent, Welcome
from ainterviewer.types import TestType
from app.db.crud import InterviewDataBase
from app.db.repositories.errors import PublishError
from app.schemas.participants import ParticipantCreate
from app.schemas.projects import ExperimentCreate, ExperimentProjectCreate
from app.schemas.testing import TestRunCreate, TestSetupCreate
from app.types import CompletionRedirects, ExperimentAllocation, ExternalParam

from . import guides
from .accounts import SeedAccounts


@dataclass(frozen=True)
class SeedProjects:
    ids: tuple[UUID, ...]


def create(
    db: InterviewDataBase, session: Session, accounts: SeedAccounts
) -> SeedProjects:
    owner = accounts.owner_id
    discovery = db.projects.create_project(
        accounts.research_folder_id, "AI research workflow discovery", owner
    )
    survey = db.projects.create_project(
        accounts.demo_folder_id, "Survey control catalogue", owner
    )
    community = db.projects.create_project(
        accounts.research_folder_id, "Bilingual neighbourhood study", owner
    )
    panel = db.projects.create_project(
        accounts.research_folder_id, "Participant panel onboarding", owner
    )
    control = db.projects.create_project(
        accounts.demo_folder_id, "Control-flow condition laboratory", owner
    )
    db.projects.update_guide(discovery, guides.discussion())
    db.projects.update_guide(survey, guides.survey_controls())
    db.projects.update_guide(control, guides.control_flow())
    _configure_community(db, session, community)
    _configure_panel(db, session, panel)
    _add_participants(db, discovery, community, panel, control)
    _add_testing(db, survey, discovery, panel, owner)
    ids = (discovery, survey, community, panel, control)
    _publish_when_possible(db, session, ids, owner)
    return SeedProjects(ids)


def _configure_community(
    db: InterviewDataBase, session: Session, project_id: UUID
) -> None:
    content = db.projects._content(project_id)
    content.languages = ["EN", "DA"]
    session.commit()
    db.projects.update_guide(project_id, guides.multilingual())
    content = db.projects._content(project_id)
    content.consent = LocalizedConsent.from_rendered(
        {
            "EN": Consent(
                title="Your consent", text="Please confirm that you agree to take part."
            ),
            "DA": Consent(
                title="Dit samtykke", text="Bekræft venligst, at du vil deltage."
            ),
        }
    )
    content.welcome = LocalizedWelcome.from_rendered(
        {
            "EN": Welcome(
                title="Welcome",
                text="Your local perspective matters.",
                email="research@example.com",
            ),
            "DA": Welcome(
                title="Velkommen",
                text="Dit lokale perspektiv er vigtigt.",
                email="research@example.com",
            ),
        },
        structure="EN",
    )
    content.emails.invite.subject = {
        "EN": "Invitation to participate",
        "DA": "Invitation til at deltage",
    }
    content.emails.invite.template = {
        "EN": "Hello,\n\nPlease share your perspective in our interview.",
        "DA": "Hej,\n\nDel gerne dit perspektiv i vores interview.",
    }
    session.commit()


def _configure_panel(db: InterviewDataBase, session: Session, project_id: UUID) -> None:
    content = db.projects._content(project_id)
    content.guide = LocalizedInterviewGuide.from_rendered(
        {
            "EN": InterviewGuide(
                framing="A short panel onboarding interview.",
                extra_variables=["panel_name"],
                introduction="Welcome to {{ panel_name }}.",
                outro="Thank you — you are ready for the panel.",
                question_sections=[
                    QuestionSection(
                        description="Eligibility and preferences.",
                        questions=[
                            Question(
                                main_question="Which research topics interest you most?"
                            ),
                            Question(
                                main_question="May we contact you about future studies?",
                                survey_item=RadioItem(options=["Yes", "No"]),
                            ),
                        ],
                    )
                ],
            )
        },
        structure="EN",
    )
    session.commit()
    db.projects.update_external_params(
        project_id,
        [
            ExternalParam(name="panel_name", type="str", description="Panel brand"),
            ExternalParam(
                name="cohort", type="enum", options=["A", "B"], required=False
            ),
        ],
    )
    db.projects.update_redirects(
        project_id,
        CompletionRedirects(
            complete="https://example.test/complete?cohort={{ cohort }}",
            screened="https://example.test/screened",
        ),
    )


def _add_participants(db: InterviewDataBase, *projects: UUID) -> None:
    people = (
        ["Alex Morgan", "Sam Lee", "Jordan Patel"],
        ["Maja Nielsen", "Freja Hansen", "Omar Ali"],
        ["Taylor Brooks", "Riley Chen"],
        ["Condition Test Participant"],
    )
    for project_id, names in zip(projects, people, strict=True):
        db.participants.add_participants(
            project_id,
            [
                ParticipantCreate(
                    name=name,
                    email=f"{name.lower().replace(' ', '.')}@example.com",
                    lang="DA" if name in {"Maja Nielsen", "Freja Hansen"} else "EN",
                )
                for name in names
            ],
        )


def _add_testing(
    db: InterviewDataBase, survey: UUID, discovery: UUID, panel: UUID, owner: UUID
) -> None:
    setup = db.tests.create_test_setup(
        TestSetupCreate(
            name="Fixed answers smoke test",
            type=TestType.FIXED_ANSWERS,
            project_id=survey,
            n_interviews=3,
        )
    )
    db.tests.update_fixed_personas(
        setup.id,
        ["A doctoral researcher", "A research operations lead"],
        db.tests.get_setup_revision(setup.id),
    )
    db.tests.create_test_run(
        TestRunCreate(test_setup_id=setup.id, n_interviews=3, answering_model=None)
    )
    db.tests.create_experiment(
        ExperimentCreate(
            title="Onboarding wording experiment",
            allocation=ExperimentAllocation.BALANCED,
            projects=[
                ExperimentProjectCreate(project_id=discovery, weight=1, target=20),
                ExperimentProjectCreate(project_id=panel, weight=1, target=20),
            ],
        ),
        owner,
    )


def _publish_when_possible(
    db: InterviewDataBase, session: Session, ids: tuple[UUID, ...], owner: UUID
) -> None:
    for project_id in ids:
        try:
            db.versions.publish(project_id, owner, label="Initial development fixture")
        except PublishError:
            # Test-only model configurations cannot publish respondent content.
            session.rollback()
