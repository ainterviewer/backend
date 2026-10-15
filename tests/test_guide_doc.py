"""The guide as a Yjs document (`app/collab/guide_doc.py`).

What matters is that a guide survives the trip into a doc and back exactly
-- versioning hashes and diffs the result -- and that the shape keeps
concurrent edits apart: two people typing in one field, a move racing an
edit, a server-side write landing while someone types.
"""

from uuid import uuid4

from pycrdt import Doc, Map, Text

from ainterviewer.interview_guides import (
    InterviewGuide,
    Question,
    QuestionSection,
    RadioItem,
    TimedMessage,
)
from ainterviewer.interview_guides.conditions import (
    Condition,
    ConditionEvaluation,
    Conditions,
    QuestionContext,
)
from ainterviewer.interview_guides.localized import LocalizedInterviewGuide
from ainterviewer.interview_guides.media import Image
from ainterviewer.interview_guides.survey_items import SliderItem
from ainterviewer.interview_guides.types import ConditionAction
from app.collab.guide_doc import (
    apply_guide,
    apply_guide_data,
    doc_id,
    merge_guide_data,
    new_doc,
    order_keys,
    read_guide,
    read_guide_data,
    roots,
)

_ids = {k: uuid4() for k in ("radio", "gated", "slider", "s1", "s2")}


def _rendered(language: str, never: str) -> InterviewGuide:
    radio = Question(
        main_question=f"{language} how often?",
        survey_item=RadioItem(options=[f"{language} often", never]),
        id=_ids["radio"],
    )
    gated = Question(
        main_question=f"{language} why?",
        probes=[f"{language} more", f"{language} else"],
        id=_ids["gated"],
        image=Image(
            name="cat.png",
            data=None,
            description=f"{language} a cat",
            alt=f"{language} cat",
        ),
        conditions=Conditions(
            action=ConditionAction.SKIP_QUESTION,
            conditions=[
                Condition(
                    question_context=QuestionContext(
                        question_index=(0, 0), question_id=radio.id
                    ),
                    evaluation=[ConditionEvaluation(trigger_value=never)],
                )
            ],
        ),
    )
    slider = Question(
        main_question=f"{language} rate 😀 it",
        survey_item=SliderItem(
            min=0, max=10, min_label=f"{language} low", max_label=f"{language} hi"
        ),
        max_probes_n=1,
        id=_ids["slider"],
    )
    return InterviewGuide(
        framing=f"{language} framing",
        introduction=f"{language} hello",
        outro=f"{language} bye",
        extra_variables=["name"],
        question_sections=[
            QuestionSection[Question](
                id=_ids["s1"], description=f"{language} one", questions=[radio, gated]
            ),
            QuestionSection[Question](
                id=_ids["s2"], description=f"{language} two", questions=[slider]
            ),
        ],
        timed_messages=[TimedMessage(message=f"{language} hurry", time=60)],
    )


def _guide() -> LocalizedInterviewGuide:
    return LocalizedInterviewGuide.from_rendered(
        {"EN": _rendered("EN", "Never"), "DA": _rendered("DA", "Aldrig")},
        structure="EN",
    )


def _dump(guide: LocalizedInterviewGuide) -> dict:
    return guide.model_dump(mode="json")


def _copy(doc: Doc) -> Doc:
    other = Doc()
    other.apply_update(doc.get_update())
    return other


def _sync(a: Doc, b: Doc) -> None:
    a_state, b_state = a.get_state(), b.get_state()
    a.apply_update(b.get_update(a_state))
    b.apply_update(a.get_update(b_state))


def _text(doc: Doc, question: str, field: str, language: str) -> Text:
    *_, questions = roots(doc)
    return questions[str(_ids[question])][field][language]


def test_a_guide_survives_the_round_trip_exactly():
    guide = _guide()
    doc = new_doc(guide)

    assert _dump(read_guide(doc)) == _dump(guide)
    assert doc_id(doc) is not None


def test_numbers_come_back_as_they_went_in():
    # Yjs has only doubles; a slider's int bounds must not come back floats.
    guide = read_guide(new_doc(_guide()))
    item = guide.question_sections[1].questions[0].survey_item
    assert item is not None and type(item.min) is int  # ty: ignore[unresolved-attribute]


def test_reapplying_the_same_guide_changes_nothing():
    guide = _guide()
    doc = new_doc(guide)
    before = doc.get_state()

    apply_guide(doc, guide)

    assert doc.get_state() == before


def test_two_people_typing_in_one_field_both_land():
    doc = new_doc(_guide())
    other = _copy(doc)

    _text(doc, "radio", "main_question", "EN").insert(0, "Ada: ")
    text = _text(other, "radio", "main_question", "EN")
    text.insert(len(str(text).encode()), " (Bo)")
    _sync(doc, other)

    assert str(_text(doc, "radio", "main_question", "EN")) == (
        "Ada: EN how often? (Bo)"
    )
    assert _dump(read_guide(doc)) == _dump(read_guide(other))


def test_a_move_racing_an_edit_keeps_both():
    doc = new_doc(_guide())
    other = _copy(doc)

    # One moves the slider question into the first section, ahead of it all...
    *_, questions = roots(doc)
    slider = questions[str(_ids["slider"])]
    slider["section_id"] = f'"{_ids["s1"]}"'
    slider["order"] = '"a"'  # sorts before "a0"
    # ...while the other types in it.
    _text(other, "slider", "main_question", "DA").insert(0, "Nu: ")
    _sync(doc, other)

    guide = read_guide(doc)
    first = guide.question_sections[0].questions
    assert [q.id for q in first] == [_ids["slider"], _ids["radio"], _ids["gated"]]
    assert first[0].main_question["DA"] == "Nu: DA rate 😀 it"
    assert guide.question_sections[1].questions == []


def test_a_server_write_keeps_what_someone_is_typing():
    guide = _guide()
    doc = new_doc(guide)
    other = _copy(doc)

    # An editor types in a question while the server translates another
    # field (a restore, a translation): applied as a diff, not a rewrite.
    _text(other, "gated", "main_question", "EN").insert(0, "So, ")
    changed = guide.model_copy(deep=True)
    changed.question_sections[0].questions[1].main_question["DA"] = "DA hvorfor?"
    changed.introduction["EN"] = "EN hello there"
    apply_guide(doc, changed)
    _sync(doc, other)

    question = read_guide(doc).question_sections[0].questions[1]
    assert question.main_question == {"EN": "So, EN why?", "DA": "DA hvorfor?"}
    assert read_guide(other).introduction["EN"] == "EN hello there"


def test_the_smallest_change_is_written_to_a_text():
    guide = _guide()
    doc = new_doc(guide)
    other = _copy(doc)
    # The other editor's caret sits after "rate"; the server edits after it.
    text = _text(other, "slider", "main_question", "EN")
    text.insert(len(b"EN rate"), "!")

    changed = guide.model_copy(deep=True)
    changed.question_sections[1].questions[0].main_question["EN"] = "EN rate 😀 it now"
    apply_guide(doc, changed)
    _sync(doc, other)

    assert str(_text(doc, "slider", "main_question", "EN")) == "EN rate! 😀 it now"


def test_removed_things_leave_the_doc():
    guide = _guide()
    doc = new_doc(guide)

    changed = guide.model_copy(deep=True)
    changed.question_sections = changed.question_sections[:1]
    changed.question_sections[0].questions[1].probes = None
    changed.outro = None
    apply_guide(doc, changed)

    _meta, top, sections, questions = roots(doc)
    assert str(_ids["s2"]) not in sections
    assert str(_ids["slider"]) not in questions
    assert "probes" not in questions[str(_ids["gated"])]
    assert "outro" not in top
    assert _dump(read_guide(doc)) == _dump(changed)


def test_a_question_whose_section_is_gone_is_left_out():
    doc = new_doc(_guide())
    other = _copy(doc)

    _meta, _top, sections, _questions = roots(doc)
    del sections[str(_ids["s2"])]
    _text(other, "slider", "main_question", "EN").insert(0, "x")
    _sync(doc, other)

    guide = read_guide(doc)
    assert [s.id for s in guide.question_sections] == [_ids["s1"]]


def test_an_emptied_language_reads_as_untranslated():
    doc = new_doc(_guide())
    text = _text(doc, "radio", "main_question", "DA")
    del text[0 : len(str(text).encode())]

    question = read_guide(doc).question_sections[0].questions[0]
    assert question.main_question == {"EN": "EN how often?"}


def test_a_new_language_text_is_added_as_text():
    guide = _guide()
    doc = new_doc(guide)
    changed = guide.model_copy(deep=True)
    changed.framing["DE"] = "DE framing"

    apply_guide(doc, changed)

    _meta, top, *_ = roots(doc)
    assert isinstance(top["framing"], Map)
    assert isinstance(top["framing"]["DE"], Text)


# ---- merging a write the doc did not make -----------------------------------


def test_a_column_write_merges_into_unsaved_edits():
    guide = _guide()
    base = _dump(guide)
    doc = new_doc(guide)
    # Typed in the doc, not yet saved...
    _text(doc, "radio", "main_question", "EN").insert(0, "So, ")
    # ...while a translation wrote the column.
    theirs = guide.model_copy(deep=True)
    theirs.question_sections[0].questions[1].main_question["DE"] = "DE warum?"
    theirs.framing["DE"] = "DE framing"

    merged = merge_guide_data(base, _dump(theirs), read_guide_data(doc))
    apply_guide_data(doc, merged)

    result = read_guide(doc)
    assert result.question_sections[0].questions[0].main_question["EN"] == (
        "So, EN how often?"
    )
    assert result.question_sections[0].questions[1].main_question["DE"] == "DE warum?"
    assert result.framing["DE"] == "DE framing"


def test_a_merge_keeps_what_either_side_added_and_drops_what_either_removed():
    guide = _guide()
    base = _dump(guide)
    ours = guide.model_copy(deep=True)
    ours.question_sections[1].questions.append(
        ours.question_sections[1]
        .questions[0]
        .model_copy(update={"id": uuid4(), "main_question": {"EN": "new"}})
    )
    theirs = guide.model_copy(deep=True)
    del theirs.question_sections[0].questions[1]

    merged = LocalizedInterviewGuide.model_validate(
        merge_guide_data(base, _dump(theirs), _dump(ours))
    )

    assert [len(s.questions) for s in merged.question_sections] == [1, 2]


def test_where_both_changed_one_text_the_column_wins():
    guide = _guide()
    base = _dump(guide)
    ours = guide.model_copy(deep=True)
    ours.introduction["EN"] = "ours"
    theirs = guide.model_copy(deep=True)
    theirs.introduction["EN"] = "theirs"

    merged = merge_guide_data(base, _dump(theirs), _dump(ours))

    assert merged["introduction"]["EN"] == "theirs"


def test_every_text_has_one_in_each_language_even_if_empty():
    # So nobody has to create one by typing into it, racing someone else.
    guide = _guide()
    doc = new_doc(guide, ["EN", "DA", "DE"])

    _meta, top, _sections, questions = roots(doc)
    assert str(top["framing"]["DE"]) == ""
    probes = questions[str(_ids["gated"])]["probes"]
    assert all(str(entry["DE"]) == "" for entry in probes)
    # And reads back as untranslated, not as written.
    assert _dump(read_guide(doc)) == _dump(guide)

    # Dropping the language drops its (empty) texts.
    apply_guide(doc, guide, ["EN", "DA"])
    assert "DE" not in top["framing"]


def test_order_keys_are_the_ones_the_editor_writes():
    # As `fractional-indexing`'s generateNKeysBetween(null, null, n), which
    # rejects keys in any other format; checked against it for 3956 keys.
    keys = order_keys(62 + 62**2 + 1)
    assert keys[:3] == ["a0", "a1", "a2"]
    assert keys[61:63] == ["az", "b00"]
    assert keys[-2:] == ["bzz", "c000"]
    assert keys == sorted(keys)
