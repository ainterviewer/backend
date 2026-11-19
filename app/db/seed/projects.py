"""Project, participant, test and experiment fixtures."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ainterviewer.interview_guides import (
    InterviewGuide,
    LikertItem,
    LocalizedConsent,
    LocalizedInterviewGuide,
    LocalizedWelcome,
    Question,
    QuestionSection,
    RadioItem,
)
from ainterviewer.interview_guides.extra import Consent, Welcome
from ainterviewer.interview_guides.interview_guide import InterviewMessage
from ainterviewer.types import (
    InterviewEndReason,
    Interviewer,
    InterviewStatus,
    LanguageCode,
    MessageRole,
    MessageType,
    TestType,
)
from app.db.crud import InterviewDataBase
from app.db.repositories.errors import PublishError
from app.db.tables import ParticipantTable, ProjectParticipantTable
from app.schemas.participants import ParticipantCreate
from app.schemas.projects import ExperimentCreate, ExperimentProjectCreate
from app.schemas.testing import TestRunCreate, TestSetupCreate
from app.types import (
    CompletionRedirects,
    ExperimentAllocation,
    ExternalParam,
    InterviewType,
)

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
    _add_interview_responses(db, session, discovery, community)
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


def _add_interview_responses(
    db: InterviewDataBase,
    session: Session,
    discovery: UUID,
    community: UUID,
) -> None:
    """Add completed respondent transcripts for the dashboard and analysis demos."""
    _add_responses_for_project(
        db,
        session,
        discovery,
        "EN",
        (
            (
                (
                    "Drafting interview guides and summarizing pilot calls",
                    "It gives me a starting point when I'm staring at a blank page.",
                    "It misses sarcasm, so I still read the transcript alongside the recording.",
                ),
                ("Yes",),
                (
                    "Link each summary claim to the exact moment in the recording.",
                    "Junior researchers could double-check their interpretations faster.",
                ),
                ("I'd like to choose which parts of a recording the tool can see.",),
            ),
            (
                (
                    "Cleaning up transcripts and tagging recurring themes",
                    "It catches obvious transcription errors before I start coding.",
                    "It merges themes that look similar but matter for different reasons.",
                ),
                ("No",),
                (
                    "Show which excerpts led to each suggested theme.",
                    "The person who inherits a study midway through would benefit first.",
                ),
                (
                    "We need to keep competing interpretations instead of overwriting them.",
                ),
            ),
            (
                (
                    "Drafting grant applications and rewriting recruitment emails",
                    "It helps me explain the same study to several different audiences.",
                    "The drafts tend to promise more than the study can deliver.",
                ),
                ("Yes",),
                (
                    "Let me pin the study facts every draft has to stick to.",
                    "Small teams without a dedicated operations person would benefit first.",
                ),
                (
                    "It would help to see how much time we save after checking the drafts.",
                ),
            ),
        ),
    )
    _add_responses_for_project(
        db,
        session,
        community,
        "DA",
        (
            (
                (
                    "Jeg sætter pris på de små butikker, hvor folk kender hinanden.",
                    "Da min cykel punkterede, lånte købmanden mig sin cykelpumpe.",
                ),
                ("Tryg",),
            ),
            (
                (
                    "Jeg kan lide blandingen af familier, studerende og ældre.",
                    "Til brætspilsaftenerne på biblioteket møder jeg folk i alle aldre.",
                ),
                ("Utryg",),
            ),
        ),
    )
    _add_responses_for_project(
        db,
        session,
        community,
        "EN",
        (
            (
                (
                    "The community garden makes it easy to meet neighbours.",
                    "Last summer we shared the tomatoes with families on our street.",
                ),
                ("Safe",),
            ),
        ),
    )


def _add_responses_for_project(
    db: InterviewDataBase,
    session: Session,
    project_id: UUID,
    language: LanguageCode,
    responses: tuple[tuple[tuple[str, ...], ...], ...],
) -> None:
    # Development fixtures can run without model credentials, which leaves a
    # project unpublished. These transcripts intentionally use the draft so
    # the seed command remains self-contained in that configuration.
    localization = db.projects.get_project_localization(project_id, language)
    guide = localization.interview_guide
    participants = session.scalars(
        select(ProjectParticipantTable.id)
        .join(ProjectParticipantTable.participant)
        .where(
            ProjectParticipantTable.project_id == project_id,
            ParticipantTable.lang == language,
        )
        .order_by(ParticipantTable.name)
    ).all()
    questions = [
        (section_index, question_index, question)
        for section_index, section in enumerate(guide.question_sections)
        for question_index, question in enumerate(section.questions)
    ]
    for participant_id, answers in zip(participants, responses, strict=True):
        question_answers = list(zip(questions, answers, strict=True))
        interview = db.interviews.create_interview(
            project_id,
            interview_guide=guide,
            interview_type=InterviewType.DISTRIBUTED,
            interviewer=Interviewer.AI,
            language=language,
            participant_id=participant_id,
            content_version_id=localization.content_version_id,
        )
        _write_transcript(db, interview.id, project_id, guide, question_answers)
        db.interviews.update_interview_status(
            project_id,
            interview.id,
            status=InterviewStatus.COMPLETED,
            time_spent=110,
            end_reason=InterviewEndReason.FINISHED,
        )


def _write_transcript(
    db: InterviewDataBase,
    interview_id: UUID,
    project_id: UUID,
    guide: InterviewGuide,
    question_answers: list[tuple[tuple[int, int, Question], tuple[str, ...]]],
) -> None:
    message_id = 0

    def insert(content: str, role: MessageRole, **fields) -> None:
        nonlocal message_id
        message_id += 1
        db.interviews.insert_message(
            message_id, content, role, interview_id, project_id, **fields
        )

    if intro := guide.introduction:
        insert(
            intro.message if isinstance(intro, InterviewMessage) else intro,
            MessageRole.ASSISTANT,
            is_introduction=True,
            can_answer=False,
        )
    main_answers: dict[tuple[int, int], str] = {}
    for (section, question_index, question), answers in question_answers:
        prompt = question.main_question
        if question.references:
            prompt = prompt.format(
                *(main_answers[ref.question_index] for ref in question.references)
            )
        main_answers[section, question_index] = answers[0]
        prompts = [prompt, *(question.probes or [])]
        for probe, (prompt, answer) in enumerate(zip(prompts, answers, strict=True)):
            survey = question.survey_item if probe == 0 else None
            if isinstance(
                survey, (RadioItem, LikertItem)
            ) and not survey.validate_answer(answer):
                raise ValueError(f"Invalid seed answer {answer!r} for {prompt!r}")
            fields = {
                "section": section,
                "main_question": question_index,
                "sub_question": probe,
                "message_type": MessageType.SURVEY_ITEM if survey else MessageType.TEXT,
            }
            insert(
                prompt,
                MessageRole.ASSISTANT,
                survey_item=survey,
                can_answer=question.can_answer,
                include_in_history=not question.exclude_from_history,
                **fields,
            )
            insert(answer, MessageRole.USER, **fields)
    if outro := guide.outro:
        insert(
            outro.message if isinstance(outro, InterviewMessage) else outro,
            MessageRole.ASSISTANT,
            outro=True,
            can_answer=False,
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
