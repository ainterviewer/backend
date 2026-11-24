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
        accounts.research_folder_id, "Multilingual neighbourhood study", owner
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
    # Everything below was added later. It runs last so that the rows above
    # keep the stable ids they had before it (see `ids.py`).
    _add_community_arabic_swahili_responses(db, session, community)
    health = _create_health(db, session, accounts)
    _publish_when_possible(db, session, (health,), owner)
    _add_health_responses(db, session, health)
    return SeedProjects((*ids, health))


def _configure_community(
    db: InterviewDataBase, session: Session, project_id: UUID
) -> None:
    content = db.projects._content(project_id)
    content.languages = ["EN", "DA", "AR", "SW"]
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
            "AR": Consent(title="موافقتك", text="يُرجى تأكيد موافقتك على المشاركة."),
            "SW": Consent(
                title="Idhini yako",
                text="Tafadhali thibitisha kwamba unakubali kushiriki.",
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
            "AR": Welcome(
                title="أهلًا وسهلًا",
                text="وجهة نظرك المحلية مهمة.",
                email="research@example.com",
            ),
            "SW": Welcome(
                title="Karibu",
                text="Mtazamo wako kuhusu eneo lako ni muhimu.",
                email="research@example.com",
            ),
        },
        structure="EN",
    )
    content.emails.invite.subject = {
        "EN": "Invitation to participate",
        "DA": "Invitation til at deltage",
        "AR": "دعوة للمشاركة",
        "SW": "Mwaliko wa kushiriki",
    }
    content.emails.invite.template = {
        "EN": "Hello,\n\nPlease share your perspective in our interview.",
        "DA": "Hej,\n\nDel gerne dit perspektiv i vores interview.",
        "AR": "مرحبًا،\n\nنرجو أن تشاركنا وجهة نظرك في مقابلتنا.",
        "SW": "Habari,\n\nTafadhali shiriki mtazamo wako katika mahojiano yetu.",
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


def _create_health(
    db: InterviewDataBase, session: Session, accounts: SeedAccounts
) -> UUID:
    """English by default, with Arabic (right to left) and Swahili."""
    project_id = db.projects.create_project(
        accounts.research_folder_id, "Community health access study", accounts.owner_id
    )
    content = db.projects._content(project_id)
    content.languages = ["EN", "AR", "SW"]
    session.commit()
    db.projects.update_guide(project_id, guides.community_health())
    content = db.projects._content(project_id)
    content.consent = LocalizedConsent.from_rendered(
        {
            "EN": Consent(
                title="Your consent",
                text=(
                    "Your answers are kept confidential. Please confirm that you "
                    "agree to take part."
                ),
            ),
            "AR": Consent(
                title="موافقتك",
                text="ستبقى إجاباتك سرية. يُرجى تأكيد موافقتك على المشاركة.",
            ),
            "SW": Consent(
                title="Idhini yako",
                text=(
                    "Majibu yako yatabaki kuwa siri. Tafadhali thibitisha kwamba "
                    "unakubali kushiriki."
                ),
            ),
        }
    )
    content.welcome = LocalizedWelcome.from_rendered(
        {
            "EN": Welcome(
                title="Welcome",
                text="Your experience helps us improve local health services.",
                email="health@example.com",
            ),
            "AR": Welcome(
                title="أهلًا وسهلًا",
                text="تساعدنا تجربتك على تحسين الخدمات الصحية المحلية.",
                email="health@example.com",
            ),
            "SW": Welcome(
                title="Karibu",
                text="Uzoefu wako unatusaidia kuboresha huduma za afya za eneo lako.",
                email="health@example.com",
            ),
        },
        structure="EN",
    )
    content.emails.invite.subject = {
        "EN": "Invitation: community health interview",
        "AR": "دعوة: مقابلة حول الصحة المجتمعية",
        "SW": "Mwaliko: mahojiano kuhusu afya ya jamii",
    }
    content.emails.invite.template = {
        "EN": "Hello,\n\nPlease tell us about your experience of local health care.",
        "AR": "مرحبًا،\n\nنرجو أن تخبرنا عن تجربتك مع الرعاية الصحية المحلية.",
        "SW": (
            "Habari,\n\nTafadhali tueleze kuhusu uzoefu wako wa huduma za afya "
            "katika eneo lako."
        ),
    }
    session.commit()
    _add_named_participants(
        db,
        project_id,
        {
            "Grace Thompson": "EN",
            "Mariam Saleh": "AR",
            "Yousef Nasser": "AR",
            "Baraka Otieno": "SW",
            "Neema Wanjiru": "SW",
        },
    )
    return project_id


def _add_named_participants(
    db: InterviewDataBase, project_id: UUID, people: dict[str, LanguageCode]
) -> None:
    db.participants.add_participants(
        project_id,
        [
            ParticipantCreate(
                name=name,
                email=f"{name.lower().replace(' ', '.')}@example.com",
                lang=lang,
            )
            for name, lang in people.items()
        ],
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


def _add_community_arabic_swahili_responses(
    db: InterviewDataBase, session: Session, community: UUID
) -> None:
    _add_named_participants(db, community, {"Layla Haddad": "AR", "Amani Mwangi": "SW"})
    _add_responses_for_project(
        db,
        session,
        community,
        "AR",
        (
            (
                (
                    "أحبّ السوق الأسبوعي، حيث يلتقي الجيران ويتبادلون الأخبار.",
                    "عندما مرضت والدتي، أحضر لنا الجيران الطعام لأسبوع كامل.",
                ),
                ("آمن",),
            ),
        ),
    )
    _add_responses_for_project(
        db,
        session,
        community,
        "SW",
        (
            (
                (
                    "Napenda jinsi majirani wanavyosaidiana wakati wa shida.",
                    (
                        "Mvua kubwa ilipoharibu paa letu, majirani walikuja "
                        "kutusaidia kulirekebisha."
                    ),
                ),
                ("Wastani",),
            ),
        ),
    )


def _add_health_responses(
    db: InterviewDataBase, session: Session, health: UUID
) -> None:
    """Answers per question: the main answer and its probe, the clinic
    distance, satisfaction, and the one change."""
    _add_responses_for_project(
        db,
        session,
        health,
        "EN",
        (
            (
                (
                    (
                        "I went to the pharmacy first, then waited two days for a "
                        "clinic appointment."
                    ),
                    "The pharmacist explained what to do while I waited, which helped.",
                ),
                ("30–60 minutes",),
                ("Neutral",),
                ("Evening opening hours, so people who work days can be seen.",),
            ),
        ),
    )
    _add_responses_for_project(
        db,
        session,
        health,
        "AR",
        (
            (  # Mariam Saleh
                (
                    "ذهبت إلى المركز الصحي في الحي، وانتظرت أكثر من ثلاث ساعات.",
                    "كانت الممرضة لطيفة، لكن قلة الأطباء جعلت الانتظار طويلًا.",
                ),
                ("من 30 إلى 60 دقيقة",),
                ("غير راضٍ",),
                ("توظيف المزيد من الأطباء في المراكز الصحية القريبة.",),
            ),
            (  # Yousef Nasser
                (
                    "اتصلت بطبيب العائلة، وحصلت على موعد في اليوم نفسه.",
                    "الموعد السريع سهّل الأمر، لكن كلفة الدواء كانت مرتفعة.",
                ),
                ("أقل من 30 دقيقة",),
                ("راضٍ",),
                ("تخفيض أسعار الأدوية لكبار السن.",),
            ),
        ),
    )
    _add_responses_for_project(
        db,
        session,
        health,
        "SW",
        (
            (  # Baraka Otieno
                (
                    (
                        "Nilisafiri hadi hospitali ya wilaya kwa basi kwa sababu "
                        "zahanati yetu haikuwa na dawa."
                    ),
                    "Gharama ya usafiri na foleni ndefu vilifanya iwe vigumu.",
                ),
                ("Zaidi ya saa moja",),
                ("Sijaridhika",),
                ("Zahanati iliyo karibu iwe na dawa za kutosha wakati wote.",),
            ),
            (  # Neema Wanjiru
                (
                    (
                        "Nilienda kwenye zahanati ya kijiji, na nikahudumiwa siku "
                        "hiyo hiyo."
                    ),
                    (
                        "Mhudumu wa afya wa jamii alinieleza mahali pa kwenda, jambo "
                        "lililorahisisha."
                    ),
                ),
                ("Dakika 30 hadi 60",),
                ("Nimeridhika",),
                ("Kliniki zifunguliwe pia wikendi kwa ajili ya watu wanaofanya kazi.",),
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
