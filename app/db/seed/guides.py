"""Interview-guide fixtures, grouped by the feature they demonstrate."""

from dataclasses import dataclass
from typing import Literal

from ainterviewer.interview_guides import (
    CheckboxItem,
    Condition,
    ConditionAction,
    ConditionBlock,
    DateItem,
    DatetimeItem,
    GridRow,
    Image,
    InterviewGuide,
    LikertGridItem,
    LikertItem,
    ListItem,
    LocalizedInterviewGuide,
    NumberItem,
    Question,
    QuestionSection,
    RadioItem,
    RankingItem,
    SliderItem,
    TimedMessage,
    TimeItem,
)
from ainterviewer.interview_guides.conditions import (
    ConditionEvaluation,
    QuestionContext,
)
from ainterviewer.interview_guides.references import Reference
from ainterviewer.interview_guides.types import ConditionTrigger


def discussion() -> LocalizedInterviewGuide:
    workflow = Question(
        main_question="Which parts of your current research workflow use AI?",
        description="Establish current practice before unmet needs.",
        probes=["What works well?", "Where does it break down?"],
        max_probes_n=2,
        check_if_exhausted=True,
        create_segue=True,
    )
    consent = Question(
        main_question="May we quote your feedback anonymously in our research?",
        survey_item=RadioItem(options=["Yes", "No"], shuffle_options=True),
    )
    follow_up = Question(
        main_question="You mentioned {}. What would an ideal improvement look like?",
        references=[Reference(question_index=(0, 0), question_id=workflow.id)],
        probes=["Who would benefit first?"],
        max_probes_n=1,
    )
    closing = Question(
        main_question="What is the most important thing we have not asked?",
        can_skip=False,
        exclude_from_history=True,
        conditions=[
            Condition(
                blocks=[
                    ConditionBlock(
                        question_context=QuestionContext(
                            question_index=(0, 1), question_id=consent.id
                        ),
                        evaluation=[ConditionEvaluation(trigger_value="No")],
                    )
                ],
                action=ConditionAction.SKIP_PROBES,
            )
        ],
    )
    return LocalizedInterviewGuide.from_rendered(
        {
            "EN": InterviewGuide(
                framing="A moderated discovery interview for a research-tool team.",
                introduction="Thanks for joining. We are interested in your experience.",
                outro="Thank you for sharing your perspective.",
                alt_outro="Thanks for your time. We will end here.",
                question_sections=[
                    QuestionSection(
                        description="Current workflow and consent.",
                        questions=[workflow, consent],
                    ),
                    QuestionSection(
                        description="Needs and opportunities.",
                        shuffle=True,
                        questions=[follow_up, closing],
                    ),
                ],
                timed_messages=[
                    TimedMessage(
                        time=120,
                        message="Take your time — there are no right answers.",
                        include_in_history=True,
                    )
                ],
            )
        },
        structure="EN",
    )


def survey_controls() -> LocalizedInterviewGuide:
    scale = ["Strongly disagree", "Disagree", "Neutral", "Agree", "Strongly agree"]
    questions = [
        (
            "Which workspace do you use most?",
            RadioItem(
                options=["Office", "Home", "Hybrid"],
                with_other=True,
                shuffle_options=True,
            ),
        ),
        (
            "Which tools do you use weekly?",
            CheckboxItem(
                options=["Notes", "Spreadsheets", "Chat", "Video"],
                with_other=True,
                shuffle_options=True,
            ),
        ),
        (
            "How confident do you feel using AI tools?",
            LikertItem(options=scale, ui="slider"),
        ),
        (
            "Please rate these aspects of your workspace.",
            LikertGridItem(
                options=scale,
                rows=[
                    GridRow(text="Privacy"),
                    GridRow(text="Ease of use"),
                    GridRow(text="Collaboration", required=False),
                ],
                shuffle_rows=True,
            ),
        ),
        (
            "Name up to three tools you would miss.",
            ListItem(min_entries=1, max_entries=3),
        ),
        (
            "Rank the improvements you value most.",
            RankingItem(
                options=["Speed", "Trust", "Cost", "Accessibility"],
                top_n=3,
                shuffle_options=True,
            ),
        ),
        (
            "How likely are you to recommend this workflow?",
            SliderItem(min=0, max=10, min_label="Not at all", max_label="Very likely"),
        ),
        (
            "About how many interviews do you conduct each month?",
            NumberItem(min=0, max=500, step=1),
        ),
        ("When did you last conduct an interview?", DateItem(min="2020-01-01")),
        ("When does your next study session begin?", DatetimeItem()),
        ("What time of day do you usually work?", TimeItem()),
    ]
    guide = InterviewGuide(
        framing="A compact catalogue of the supported survey controls.",
        introduction="This guide demonstrates each response format.",
        outro="That completes the survey-control demo.",
        question_sections=[
            QuestionSection(
                description="Choice and rating controls.",
                questions=[
                    Question(main_question=text, survey_item=item)
                    for text, item in questions[:4]
                ],
            ),
            QuestionSection(
                description="Structured free-form and numeric controls.",
                questions=[
                    Question(main_question=text, survey_item=item)
                    for text, item in questions[4:]
                ],
            ),
        ],
    )
    return LocalizedInterviewGuide.from_rendered({"EN": guide}, structure="EN")


def multilingual() -> LocalizedInterviewGuide:
    english = InterviewGuide(
        framing="A multilingual community feedback study.",
        introduction="Welcome — we would like to hear about your neighbourhood.",
        outro="Thank you for your contribution.",
        question_sections=[
            QuestionSection(
                description="Daily life in the neighbourhood.",
                questions=[
                    Question(
                        main_question="What do you value most about your neighbourhood?",
                        probes=["Can you give an example?"],
                    ),
                    Question(
                        main_question="How safe do you feel after dark?",
                        survey_item=LikertItem(
                            options=[
                                "Very unsafe",
                                "Unsafe",
                                "Neutral",
                                "Safe",
                                "Very safe",
                            ]
                        ),
                    ),
                ],
            )
        ],
    )
    translations = {
        "DA": _GuideText(
            framing="En flersproget undersøgelse af lokalsamfundet.",
            introduction="Velkommen — vi vil gerne høre om dit nabolag.",
            outro="Tak for dit bidrag.",
            sections=["Hverdagsliv i nabolaget."],
            questions=[
                (
                    "Hvad sætter du mest pris på ved dit nabolag?",
                    ["Kan du give et eksempel?"],
                ),
                ("Hvor tryg føler du dig efter mørkets frembrud?", []),
            ],
            options=[None, ["Meget utryg", "Utryg", "Neutral", "Tryg", "Meget tryg"]],
        ),
        "AR": _GuideText(
            framing="دراسة متعددة اللغات لآراء المجتمع المحلي.",
            introduction="مرحبًا — نودّ أن نسمع عن حيّك.",
            outro="شكرًا لمساهمتك.",
            sections=["الحياة اليومية في الحي."],
            questions=[
                ("ما أكثر ما تقدّره في حيّك؟", ["هل يمكنك أن تعطي مثالًا؟"]),
                ("ما مدى شعورك بالأمان بعد حلول الظلام؟", []),
            ],
            options=[None, ["غير آمن إطلاقًا", "غير آمن", "محايد", "آمن", "آمن جدًا"]],
        ),
        "SW": _GuideText(
            framing="Utafiti wa lugha nyingi kuhusu maoni ya jamii.",
            introduction="Karibu — tungependa kusikia kuhusu mtaa wako.",
            outro="Asante kwa mchango wako.",
            sections=["Maisha ya kila siku mtaani."],
            questions=[
                (
                    "Ni nini unachothamini zaidi kuhusu mtaa wako?",
                    ["Unaweza kutoa mfano?"],
                ),
                ("Unajisikia salama kiasi gani baada ya giza kuingia?", []),
            ],
            options=[
                None,
                ["Si salama kabisa", "Si salama", "Wastani", "Salama", "Salama sana"],
            ],
        ),
    }
    return _localize(english, translations)


def community_health() -> LocalizedInterviewGuide:
    """English, Arabic and Swahili: a right-to-left language beside two others."""
    english = InterviewGuide(
        framing="A short interview about access to health care in the community.",
        introduction=(
            "Welcome, and thank you for joining. We would like to learn how you "
            "get health care."
        ),
        outro="Thank you for sharing your experience.",
        question_sections=[
            QuestionSection(
                description="Getting care.",
                questions=[
                    Question(
                        main_question=(
                            "When you last needed medical care, how did you get it?"
                        ),
                        probes=["What made that easier or harder?"],
                        max_probes_n=1,
                    ),
                    Question(
                        main_question=(
                            "How far is the nearest health clinic from your home?"
                        ),
                        survey_item=RadioItem(
                            options=[
                                "Under 30 minutes",
                                "30–60 minutes",
                                "Over an hour",
                            ]
                        ),
                    ),
                ],
            ),
            QuestionSection(
                description="Experiences and priorities.",
                questions=[
                    Question(
                        main_question=(
                            "How satisfied are you with the health services in "
                            "your area?"
                        ),
                        survey_item=LikertItem(
                            options=[
                                "Very dissatisfied",
                                "Dissatisfied",
                                "Neutral",
                                "Satisfied",
                                "Very satisfied",
                            ]
                        ),
                    ),
                    Question(
                        main_question=(
                            "What one change would most improve health care in "
                            "your community?"
                        ),
                    ),
                ],
            ),
        ],
    )
    translations = {
        "AR": _GuideText(
            framing="مقابلة قصيرة حول الوصول إلى الرعاية الصحية في المجتمع.",
            introduction=(
                "مرحبًا، وشكرًا لانضمامك. نودّ أن نعرف كيف تحصل على الرعاية الصحية."
            ),
            outro="شكرًا لمشاركتك تجربتك.",
            sections=["الحصول على الرعاية.", "التجارب والأولويات."],
            questions=[
                (
                    "في آخر مرة احتجت فيها إلى رعاية طبية، كيف حصلت عليها؟",
                    ["ما الذي جعل ذلك أسهل أو أصعب؟"],
                ),
                ("كم تبعد أقرب عيادة صحية عن منزلك؟", []),
                ("ما مدى رضاك عن الخدمات الصحية في منطقتك؟", []),
                ("ما أهم تغيير يمكن أن يحسّن الرعاية الصحية في مجتمعك؟", []),
            ],
            options=[
                None,
                ["أقل من 30 دقيقة", "من 30 إلى 60 دقيقة", "أكثر من ساعة"],
                ["غير راضٍ إطلاقًا", "غير راضٍ", "محايد", "راضٍ", "راضٍ جدًا"],
                None,
            ],
        ),
        "SW": _GuideText(
            framing=(
                "Mahojiano mafupi kuhusu upatikanaji wa huduma za afya katika jamii."
            ),
            introduction=(
                "Karibu, na asante kwa kujiunga nasi. Tungependa kujua jinsi "
                "unavyopata huduma za afya."
            ),
            outro="Asante kwa kushiriki uzoefu wako.",
            sections=["Kupata huduma.", "Uzoefu na vipaumbele."],
            questions=[
                (
                    "Mara ya mwisho ulipohitaji huduma ya matibabu, uliipataje?",
                    ["Ni nini kilichofanya iwe rahisi au vigumu zaidi?"],
                ),
                (
                    (
                        "Kliniki ya afya iliyo karibu zaidi iko umbali gani kutoka "
                        "nyumbani kwako?"
                    ),
                    [],
                ),
                ("Umeridhika kiasi gani na huduma za afya katika eneo lako?", []),
                (
                    (
                        "Ni mabadiliko gani yangeboresha zaidi huduma za afya katika "
                        "jamii yako?"
                    ),
                    [],
                ),
            ],
            options=[
                None,
                ["Chini ya dakika 30", "Dakika 30 hadi 60", "Zaidi ya saa moja"],
                [
                    "Sijaridhika kabisa",
                    "Sijaridhika",
                    "Wastani",
                    "Nimeridhika",
                    "Nimeridhika sana",
                ],
                None,
            ],
        ),
    }
    return _localize(english, translations)


def concept_test() -> LocalizedInterviewGuide:
    """Two guide images, each shown with the main question that asks about
    it. The files are in `seed/images/`, copied into the project's image
    directory under these names (`projects._create_concept_test`)."""
    clarity = [
        "Very unclear",
        "Unclear",
        "Neither clear nor unclear",
        "Clear",
        "Very clear",
    ]
    label_a = Question(
        main_question="What is your first impression of this label?",
        image=Image(
            name="recycling-label-a.svg",
            primer="Here is the first label we are considering for the bins.",
            description=(
                "Concept A, led by pictograms: a green band with the recycling "
                "symbol, line drawings of a bottle, a can and a carton, and the "
                "words 'Plastic & metal' in small grey type underneath. It lists "
                "nothing that does not belong in the bin."
            ),
            alt=(
                "A bin label with a green header, drawings of a bottle, a can and "
                "a carton, and the caption 'Plastic & metal'."
            ),
        ),
        probes=["Looking at it, what would you put in this bin?"],
        max_probes_n=1,
    )
    label_b = Question(
        main_question="And what is your first impression of this one?",
        image=Image(
            name="recycling-label-b.svg",
            primer="Here is the second label.",
            description=(
                "Concept B, led by text: the heading 'Plastic & metal', a 'Yes, "
                "please' list (rinsed bottles and tubs, cans, cartons, lids "
                "screwed on), a 'No, thanks' list (plastic bags, batteries, food "
                "leftovers) and the note 'Not sure? Leave it out'. No pictures."
            ),
            alt=(
                "A bin label headed 'Plastic & metal' with a list of what goes in "
                "and a list of what does not."
            ),
        ),
        probes=["Is there anything on it you would not have expected?"],
        max_probes_n=1,
    )
    guide = InterviewGuide(
        framing=(
            "A concept test of two labels for household recycling bins, run for "
            "a municipal waste service."
        ),
        introduction=(
            "Thanks for taking part. We will show you two designs for the label "
            "on your recycling bin and ask what you make of each."
        ),
        outro="Thank you — your answers will shape the label on your bin.",
        question_sections=[
            QuestionSection(
                description="Each label on its own.",
                questions=[
                    label_a,
                    Question(
                        main_question="How clear is it what goes in this bin?",
                        survey_item=LikertItem(options=clarity),
                    ),
                    label_b,
                    Question(
                        main_question="How clear is it what goes in this bin?",
                        survey_item=LikertItem(options=clarity),
                    ),
                ],
            ),
            QuestionSection(
                description="Comparing the two.",
                questions=[
                    Question(
                        main_question="Which label would you rather have on your bin?",
                        survey_item=RadioItem(
                            options=["The first", "The second", "No preference"]
                        ),
                    ),
                    Question(
                        main_question="What would you change about the one you chose?",
                        probes=["Why that?"],
                        max_probes_n=1,
                    ),
                ],
            ),
        ],
    )
    return LocalizedInterviewGuide.from_rendered({"EN": guide}, structure="EN")


@dataclass(frozen=True)
class _GuideText:
    """One language's wording of a guide whose structure is the English one.

    `questions` and `options` run over every question in order, across
    sections; `options` is None for a question without a choice item."""

    framing: str
    introduction: str
    outro: str
    sections: list[str]
    questions: list[tuple[str, list[str]]]
    options: list[list[str] | None]


def _localize(
    english: InterviewGuide, translations: dict[str, _GuideText]
) -> LocalizedInterviewGuide:
    """Render `english` in each language by copying it, so every language
    shares its structure (and its question and option ids)."""
    rendered = {"EN": english}
    for language, text in translations.items():
        guide = english.model_copy(deep=True)
        guide.framing, guide.introduction, guide.outro = (
            text.framing,
            text.introduction,
            text.outro,
        )
        questions = [
            q for section in guide.question_sections for q in section.questions
        ]
        for section, description in zip(
            guide.question_sections, text.sections, strict=True
        ):
            section.description = description
        for question, (main, probes), options in zip(
            questions, text.questions, text.options, strict=True
        ):
            question.main_question = main
            if question.probes:
                question.probes = probes
            if options is not None:
                item = question.survey_item
                assert isinstance(item, (RadioItem, LikertItem)), main
                question.survey_item = item.model_copy(update={"options": options})
        rendered[language] = guide
    return LocalizedInterviewGuide.from_rendered(rendered, structure="EN")


def _when(action: ConditionAction, *blocks: ConditionBlock) -> Condition:
    return Condition(blocks=list(blocks), action=action)


def _reads(
    question: Question,
    *evaluations: ConditionEvaluation,
    part: Literal["main", "probes", "both"] = "main",
    **block,
) -> ConditionBlock:
    """A block reading `question`. The guide derives `question_index` from the
    id when it is validated, so the placeholder index here never matters."""
    return ConditionBlock(
        question_context=QuestionContext(
            question_index=(0, 0), question_id=question.id, part=part
        ),
        evaluation=list(evaluations),
        **block,
    )


def _is(
    value: str | float,
    operator: Literal["==", "<", "<=", ">", ">="] = "==",
    **evaluation,
) -> ConditionEvaluation:
    return ConditionEvaluation(
        trigger_value=value, comparison_operator=operator, **evaluation
    )


def control_flow() -> LocalizedInterviewGuide:
    """Every condition action plus classifier, compound and negated rules, and
    the shapes the flow view draws as alternatives: numeric, choice, ordinal,
    checkbox and open-answer forks, exhaustive or not, and inside a section
    that may be skipped."""
    skip_question = ConditionAction.SKIP_QUESTION
    satisfaction_scale = [
        "Very dissatisfied",
        "Dissatisfied",
        "Neutral",
        "Satisfied",
        "Very satisfied",
    ]

    # Section 1: the answers everything else reads.
    consent = Question(
        main_question="Do you consent to continue with this practice interview?",
        survey_item=RadioItem(options=["Yes", "No"]),
    )
    consent.conditions = [
        _when(ConditionAction.END_INTERVIEW, _reads(consent, _is("No")))
    ]
    role = Question(
        main_question="Which description fits you best?",
        survey_item=RadioItem(
            options=["Student", "Researcher", "Practitioner"], with_other=True
        ),
    )
    age = Question(
        main_question="How old are you?", survey_item=NumberItem(min=0, max=120, step=1)
    )
    age.conditions = [_when(ConditionAction.SKIP_PROBES, _reads(age, _is(18, "<")))]
    urgency = Question(
        main_question="Briefly describe the problem you want to solve today.",
        probes=["What makes it urgent?", "What would a good outcome be?"],
        max_probes_n=2,
    )
    tools = Question(
        main_question="Which tools do you use weekly?",
        survey_item=CheckboxItem(options=["Notes", "Spreadsheets", "Chat", "Video"]),
        can_skip=False,
    )
    team = Question(
        main_question="Do you work in a team? Please answer yes or no.",
    )

    # Section 2: single conditions, and forks on a number and on a choice.
    professional = Question(
        main_question="Tell us about the research team you work with.",
        conditions=[_when(skip_question, _reads(role, _is("Student")))],
    )
    sensitive = Question(
        main_question="What constraints make the problem difficult?",
        probes=["Which constraint matters most?"],
        max_probes_n=1,
        conditions=[
            _when(
                skip_question,
                _reads(
                    urgency,
                    _is("The respondent says the situation is urgent or unsafe."),
                    part="both",
                    trigger_type=ConditionTrigger.CLASSIFICATION,
                ),
            )
        ],
    )
    # Exactly one of these two, by age.
    school_tools = Question(
        main_question="What would make it easier to try a new tool at school?",
        conditions=[_when(skip_question, _reads(age, _is(18, ">=")))],
    )
    work_tools = Question(
        main_question="What would make it easier to try a new tool at work?",
        conditions=[_when(skip_question, _reads(age, _is(18, "<")))],
    )
    research_tools = Question(
        main_question="Which tools do you use for research or coursework?",
        conditions=[
            _when(
                skip_question,
                _reads(
                    role,
                    _is("Student", combine_next="OR"),
                    _is("Researcher"),
                    negated=True,
                ),
            )
        ],
    )
    # One question per role: the first two pair up as alternatives (a
    # practitioner gets neither), and the third stands on its own.
    student_course = Question(
        main_question="Which course or programme are you enrolled in?",
        conditions=[_when(skip_question, _reads(role, _is("Student"), negated=True))],
    )
    researcher_field = Question(
        main_question="What is your field of research?",
        conditions=[
            _when(skip_question, _reads(role, _is("Researcher"), negated=True))
        ],
    )
    practitioner_sector = Question(
        main_question="Which sector do you work in?",
        conditions=[
            _when(skip_question, _reads(role, _is("Practitioner"), negated=True))
        ],
    )

    # Section 3: section-level and compound conditions.
    advanced = Question(
        main_question="What would make an advanced workflow worthwhile?",
        conditions=[_when(ConditionAction.SKIP_SECTION, _reads(age, _is(18, "<")))],
    )
    decisions = Question(
        main_question="Who makes the final decision about adopting a tool?",
        conditions=[
            _when(
                skip_question,
                _reads(role, _is("Researcher"), negated=True, combine_next="OR"),
                _reads(age, _is(18, "<=")),
            )
        ],
    )
    # (Practitioner AND 30 or older) OR NOT (Notes OR Spreadsheets): blocks
    # fold left to right, and the negated block groups its two checks.
    budget = Question(
        main_question="How is spending on new tools approved where you work?",
        conditions=[
            _when(
                skip_question,
                _reads(role, _is("Practitioner"), combine_next="AND"),
                _reads(age, _is(30, ">="), combine_next="OR"),
                _reads(
                    tools,
                    _is("Notes", combine_next="OR"),
                    _is("Spreadsheets"),
                    negated=True,
                ),
            )
        ],
    )
    first_change = Question(
        main_question="What would you change first about your workflow?",
        probes=["Why that first?"],
        max_probes_n=1,
        conditions=[
            _when(
                ConditionAction.SKIP_PROBES,
                _reads(
                    urgency,
                    _is("The respondent mentions cost or budget."),
                    part="probes",
                    trigger_type=ConditionTrigger.CLASSIFICATION,
                ),
            )
        ],
    )
    young_student = Question(
        main_question="Would you like to join a follow-up session?",
        survey_item=RadioItem(options=["Yes", "No"]),
        conditions=[
            _when(
                ConditionAction.END_INTERVIEW,
                _reads(role, _is("Student"), combine_next="AND"),
                _reads(age, _is(16, "<")),
            )
        ],
    )

    # One of these two, unless the role was typed into "Other", which skips
    # neither.
    academic_topic = Question(
        main_question="What do you study or research?",
        conditions=[_when(skip_question, _reads(role, _is("Practitioner")))],
    )
    work_sector = Question(
        main_question="Which sector does your organisation work in?",
        conditions=[
            _when(
                skip_question,
                _reads(role, _is("Student", combine_next="OR"), _is("Researcher")),
            )
        ],
    )

    # Section 4: forks inside a section the respondent may skip, so even a
    # required source may go unanswered.
    gate = Question(
        main_question="Do you have a few more minutes for optional questions?",
        survey_item=RadioItem(options=["Yes", "No"]),
        can_skip=False,
    )
    gate.conditions = [_when(ConditionAction.SKIP_SECTION, _reads(gate, _is("No")))]
    satisfaction = Question(
        main_question="How satisfied are you with your current tools?",
        survey_item=LikertItem(options=satisfaction_scale),
        can_skip=False,
    )
    # Exactly one of these two, by place on the Likert scale; the second also
    # finishes early on its own answer.
    frustrations = Question(
        main_question="What frustrates you about your current tools?",
        conditions=[
            _when(
                skip_question,
                _reads(satisfaction, _is("Neutral", ">=", scale=satisfaction_scale)),
            )
        ],
    )
    highlights = Question(
        main_question="What do you like most about your current tools?",
        probes=["Can you give an example?"],
        max_probes_n=1,
    )
    highlights.conditions = [
        _when(
            skip_question,
            _reads(satisfaction, _is("Neutral", "<", scale=satisfaction_scale)),
        ),
        _when(skip_question, _reads(highlights, _is("Nothing"))),
    ]
    # Reads the first alternative above.
    switching = Question(
        main_question="What would make you switch tools?",
        probes=["What would hold you back?"],
        max_probes_n=1,
        conditions=[
            _when(
                ConditionAction.SKIP_PROBES,
                _reads(
                    frustrations,
                    _is("The respondent mentions price."),
                    part="both",
                    trigger_type=ConditionTrigger.CLASSIFICATION,
                ),
            )
        ],
    )
    # Exactly one of these two, on an open answer.
    team_roles = Question(
        main_question="What role do you play in your team?",
        conditions=[_when(skip_question, _reads(team, _is("yes"), negated=True))],
    )
    solo_work = Question(
        main_question="How do you get feedback when working on your own?",
        conditions=[_when(skip_question, _reads(team, _is("yes")))],
    )
    # At most one of these two: someone using chat and something else gets
    # neither, which only "and nothing else" tells apart.
    chat_only = Question(
        main_question="What keeps you from using tools other than chat?",
        conditions=[
            _when(
                skip_question,
                _reads(tools, _is("Chat"), negated=True, exclusive=True),
            )
        ],
    )
    no_chat = Question(
        main_question="Why don't you use chat tools?",
        conditions=[_when(skip_question, _reads(tools, _is("Chat")))],
    )

    guide = InterviewGuide(
        framing="A deliberately branch-heavy guide for testing interview control flow.",
        introduction="This is a condition-control-flow fixture. Try different answers to explore each path.",
        outro="You reached the normal end of the control-flow fixture.",
        alt_outro="The control-flow fixture ended early.",
        question_sections=[
            QuestionSection(
                description="Eligibility and branch inputs.",
                questions=[consent, role, age, urgency, tools, team],
            ),
            QuestionSection(
                description="Conditional individual questions.",
                questions=[
                    professional,
                    sensitive,
                    school_tools,
                    work_tools,
                    research_tools,
                    student_course,
                    researcher_field,
                    practitioner_sector,
                ],
            ),
            QuestionSection(
                description="Section-level and compound conditions.",
                questions=[
                    advanced,
                    decisions,
                    budget,
                    first_change,
                    young_student,
                    academic_topic,
                    work_sector,
                ],
            ),
            QuestionSection(
                description="Optional alternatives.",
                questions=[
                    gate,
                    satisfaction,
                    frustrations,
                    highlights,
                    switching,
                    team_roles,
                    solo_work,
                    chat_only,
                    no_chat,
                ],
            ),
        ],
    )
    return LocalizedInterviewGuide.from_rendered({"EN": guide}, structure="EN")


def demo_starter() -> LocalizedInterviewGuide:
    """A short draft for the demo account to test and edit."""
    return LocalizedInterviewGuide.from_rendered(
        {
            "EN": InterviewGuide(
                framing="A short interview about how people plan their week.",
                introduction="Thanks for trying this out. It takes about five minutes.",
                outro="Thank you, that was all.",
                question_sections=[
                    QuestionSection(
                        description="Planning habits.",
                        questions=[
                            Question(
                                main_question="How do you usually plan your week?",
                                probes=["What tools do you use, if any?"],
                                max_probes_n=2,
                            ),
                            Question(
                                main_question="How satisfied are you with how you plan?",
                                survey_item=LikertItem(
                                    options=[
                                        "Very dissatisfied",
                                        "Dissatisfied",
                                        "Neutral",
                                        "Satisfied",
                                        "Very satisfied",
                                    ]
                                ),
                            ),
                            Question(
                                main_question="What would make planning easier for you?",
                                probes=["Can you give an example?"],
                                max_probes_n=1,
                            ),
                        ],
                    )
                ],
            )
        },
        structure="EN",
    )
