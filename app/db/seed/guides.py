"""Interview-guide fixtures, grouped by the feature they demonstrate."""

from dataclasses import dataclass

from ainterviewer.interview_guides import (
    CheckboxItem,
    Condition,
    ConditionAction,
    ConditionBlock,
    DateItem,
    DatetimeItem,
    GridRow,
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


def control_flow() -> LocalizedInterviewGuide:
    """Every condition action plus classifier, compound and negated rules."""
    consent = Question(
        main_question="Do you consent to continue with this practice interview?",
        survey_item=RadioItem(options=["Yes", "No"]),
    )
    role = Question(
        main_question="Which description fits you best?",
        survey_item=RadioItem(options=["Student", "Researcher", "Practitioner"]),
    )
    age = Question(
        main_question="How old are you?", survey_item=NumberItem(min=0, max=120, step=1)
    )
    urgency = Question(
        main_question="Briefly describe the problem you want to solve today.",
        probes=["What makes it urgent?", "What would a good outcome be?"],
        max_probes_n=2,
    )
    context = lambda index, question, part="main": QuestionContext(
        question_index=index, question_id=question.id, part=part
    )
    consent.conditions = [
        Condition(
            blocks=[
                ConditionBlock(
                    question_context=context((0, 0), consent),
                    evaluation=[ConditionEvaluation(trigger_value="No")],
                )
            ],
            action=ConditionAction.END_INTERVIEW,
        )
    ]
    age.conditions = [
        Condition(
            blocks=[
                ConditionBlock(
                    question_context=context((0, 2), age),
                    evaluation=[
                        ConditionEvaluation(trigger_value=18, comparison_operator="<")
                    ],
                )
            ],
            action=ConditionAction.SKIP_PROBES,
        )
    ]
    professional = Question(
        main_question="Tell us about the research team you work with.",
        conditions=[
            Condition(
                blocks=[
                    ConditionBlock(
                        question_context=context((0, 1), role),
                        evaluation=[ConditionEvaluation(trigger_value="Student")],
                    )
                ],
                action=ConditionAction.SKIP_QUESTION,
            )
        ],
    )
    sensitive = Question(
        main_question="What constraints make the problem difficult?",
        probes=["Which constraint matters most?"],
        max_probes_n=1,
        conditions=[
            Condition(
                blocks=[
                    ConditionBlock(
                        question_context=context((0, 3), urgency, "both"),
                        trigger_type=ConditionTrigger.CLASSIFICATION,
                        evaluation=[
                            ConditionEvaluation(
                                trigger_value="The respondent says the situation is urgent or unsafe."
                            )
                        ],
                    )
                ],
                action=ConditionAction.SKIP_QUESTION,
            )
        ],
    )
    school_tools = Question(
        main_question="What would make it easier to try a new tool at school?",
        conditions=[
            Condition(
                blocks=[
                    ConditionBlock(
                        question_context=context((0, 2), age),
                        evaluation=[
                            ConditionEvaluation(
                                trigger_value=18, comparison_operator=">="
                            )
                        ],
                    )
                ],
                action=ConditionAction.SKIP_QUESTION,
            )
        ],
    )
    work_tools = Question(
        main_question="What would make it easier to try a new tool at work?",
        conditions=[
            Condition(
                blocks=[
                    ConditionBlock(
                        question_context=context((0, 2), age),
                        evaluation=[
                            ConditionEvaluation(
                                trigger_value=18, comparison_operator="<"
                            )
                        ],
                    )
                ],
                action=ConditionAction.SKIP_QUESTION,
            )
        ],
    )
    research_tools = Question(
        main_question="Which tools do you use for research or coursework?",
        conditions=[
            Condition(
                blocks=[
                    ConditionBlock(
                        question_context=context((0, 1), role),
                        evaluation=[
                            ConditionEvaluation(
                                trigger_value="Student", combine_next="OR"
                            ),
                            ConditionEvaluation(trigger_value="Researcher"),
                        ],
                        negated=True,
                    )
                ],
                action=ConditionAction.SKIP_QUESTION,
            )
        ],
    )
    advanced = Question(
        main_question="What would make an advanced workflow worthwhile?",
        conditions=[
            Condition(
                blocks=[
                    ConditionBlock(
                        question_context=context((0, 2), age),
                        evaluation=[
                            ConditionEvaluation(
                                trigger_value=18, comparison_operator="<"
                            )
                        ],
                    )
                ],
                action=ConditionAction.SKIP_SECTION,
            )
        ],
    )
    decisions = Question(
        main_question="Who makes the final decision about adopting a tool?",
        conditions=[
            Condition(
                blocks=[
                    ConditionBlock(
                        question_context=context((0, 1), role),
                        evaluation=[ConditionEvaluation(trigger_value="Researcher")],
                        negated=True,
                        combine_next="OR",
                    ),
                    ConditionBlock(
                        question_context=context((0, 2), age),
                        evaluation=[
                            ConditionEvaluation(
                                trigger_value=18, comparison_operator="<="
                            )
                        ],
                    ),
                ],
                action=ConditionAction.SKIP_QUESTION,
            )
        ],
    )
    guide = InterviewGuide(
        framing="A deliberately branch-heavy guide for testing interview control flow.",
        introduction="This is a condition-control-flow fixture. Try different answers to explore each path.",
        outro="You reached the normal end of the control-flow fixture.",
        alt_outro="The control-flow fixture ended early.",
        question_sections=[
            QuestionSection(
                description="Eligibility and branch inputs.",
                questions=[consent, role, age, urgency],
            ),
            QuestionSection(
                description="Conditional individual questions.",
                questions=[
                    professional,
                    sensitive,
                    school_tools,
                    work_tools,
                    research_tools,
                ],
            ),
            QuestionSection(
                description="Section-level and compound conditions.",
                questions=[advanced, decisions],
            ),
        ],
    )
    return LocalizedInterviewGuide.from_rendered({"EN": guide}, structure="EN")
