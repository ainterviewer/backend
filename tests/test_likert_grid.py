"""Likert grids across the backend: the collaborative doc, versioning,
cohort filters, transcripts and resumed interviews.

A grid is one question with one answer, stored as JSON keyed by row id, so
what is tested here is every place that has to read that answer as ratings
rather than as text.
"""

import datetime
import uuid
from zoneinfo import ZoneInfo

from pycrdt import Doc

from ainterviewer.interview_guides import GridRow, InterviewGuide, LikertGridItem
from ainterviewer.interview_guides.localized import (
    LocalizedInterviewGuide,
    LocalizedLikertGridItem,
)
from ainterviewer.types import InterviewStatus, MessageRole
from app.collab.guide_doc import new_doc, read_guide, roots
from app.collab.parts import _validate_guide
from app.db.survey_answers import AnswerRow, values_of
from app.db.versioning import Bump, _guide_bump
from app.schemas.interviews import InterviewSummaryPublic, MessagePublic
from app.services.exports.transcripts import InterviewExport, build_view
from app.types import InterviewType
from app.utils import replay_history

QUESTION = uuid.UUID(int=10)
SCALE = ["Very dissatisfied", "Dissatisfied", "Neutral", "Satisfied"]
ROWS = [
    GridRow(id=uuid.UUID(int=1), text="Cleanliness"),
    GridRow(id=uuid.UUID(int=2), text="Signage", required=False),
]


def _item() -> LikertGridItem:
    return LikertGridItem(options=SCALE, rows=[row.model_copy() for row in ROWS])


def _guide() -> LocalizedInterviewGuide:
    rendered = InterviewGuide.model_validate(
        {
            "question_sections": [
                {
                    "description": "Airport",
                    "questions": [
                        {
                            "id": str(QUESTION),
                            "main_question": "How satisfied were you with…",
                            "survey_item": _item().model_dump(mode="json"),
                            "max_probes_n": 1,
                        }
                    ],
                }
            ]
        }
    )
    return LocalizedInterviewGuide.from_rendered({"EN": rendered}, structure="EN")


def _grid(guide: LocalizedInterviewGuide) -> LocalizedLikertGridItem:
    item = guide.question_sections[0].questions[0].survey_item
    assert isinstance(item, LocalizedLikertGridItem)
    return item


def _dump(guide: LocalizedInterviewGuide) -> dict:
    return guide.model_dump(mode="json")


# ── The collaborative doc ────────────────────────────────────────────────────


def test_a_grid_survives_the_doc_round_trip():
    guide = _guide()
    assert _dump(read_guide(new_doc(guide))) == _dump(guide)


def test_two_people_typing_in_a_row_both_land():
    doc = new_doc(_guide())
    other = Doc()
    other.apply_update(doc.get_update())

    def row_text(d: Doc):
        _meta, _top, _sections, questions = roots(d)
        return questions[str(QUESTION)]["survey_item"]["rows"][str(ROWS[0].id)]["text"][
            "EN"
        ]

    row_text(doc).insert(0, "Overall ")
    text = row_text(other)
    text.insert(len(str(text).encode()), " of toilets")

    a, b = doc.get_state(), other.get_state()
    doc.apply_update(other.get_update(a))
    other.apply_update(doc.get_update(b))

    assert str(row_text(doc)) == "Overall Cleanliness of toilets"
    assert _dump(read_guide(doc)) == _dump(read_guide(other))


def test_a_grid_saves():
    guide, errors = _validate_guide(_dump(_guide()), "EN")
    assert guide is not None and errors == []


# ── Versioning ───────────────────────────────────────────────────────────────


def test_removing_a_row_is_major():
    old = _guide()
    new = old.model_copy(deep=True)
    _grid(new).rows.pop()
    assert _guide_bump(old, new) == Bump.MAJOR


def test_removing_a_scale_point_is_major():
    old = _guide()
    new = old.model_copy(deep=True)
    _grid(new).options.pop()
    assert _guide_bump(old, new) == Bump.MAJOR


def test_adding_or_reordering_rows_is_minor():
    old = _guide()
    added = old.model_copy(deep=True)
    _grid(added).rows.append(
        _grid(added).rows[0].model_copy(update={"id": uuid.uuid4()})
    )
    assert _guide_bump(old, added) == Bump.MINOR

    moved = old.model_copy(deep=True)
    _grid(moved).rows.reverse()
    assert _guide_bump(old, moved) == Bump.MINOR


def test_rewording_a_row_is_not_major():
    old = _guide()
    new = old.model_copy(deep=True)
    _grid(new).rows[0].text["EN"] = "Tidiness"
    assert _guide_bump(old, new) < Bump.MAJOR


def test_a_grid_is_not_a_cohort_filter_value():
    row = AnswerRow(
        interview_id=uuid.UUID(int=0),
        coordinate=(0, 0),
        content=_item().encode_answer({ROWS[0].id: "Neutral"}),
        item=_item(),
        language="EN",
    )
    assert values_of(row) == []


# ── Transcripts ──────────────────────────────────────────────────────────────


def test_a_transcript_shows_the_ratings():
    interview_id = uuid.uuid4()
    started = datetime.datetime(2026, 11, 10, 10, tzinfo=datetime.UTC)
    item = _item()

    def message(message_id, role, content, **extra) -> MessagePublic:
        return MessagePublic(
            id=uuid.uuid4(),
            message_id=message_id,
            content=content,
            role=role,
            interview_id=interview_id,
            project_id=uuid.uuid4(),
            created_at=started,
            interview_type=InterviewType.DISTRIBUTED,
            section=0,
            **extra,
        )

    view = build_view(
        InterviewExport(
            summary=InterviewSummaryPublic(
                id=interview_id,
                language="EN",
                status=InterviewStatus.COMPLETED,
                type=InterviewType.DISTRIBUTED,
                created_at=started,
                n_messages=2,
            ),
            messages=[
                message(
                    0,
                    MessageRole.ASSISTANT,
                    "How satisfied were you with…",
                    main_question=0,
                    sub_question=0,
                    survey_item=item,
                ),
                message(
                    1,
                    MessageRole.USER,
                    item.encode_answer(
                        {ROWS[0].id: "Satisfied", ROWS[1].id: "Neutral"}
                    ),
                ),
            ],
        ),
        ZoneInfo("UTC"),
    )
    answer = view.turns[-1]
    assert answer.text == "Cleanliness: Satisfied\nSignage: Neutral"
    assert answer.survey is not None
    assert answer.survey.kind == "Likert grid"
    assert answer.survey.answer == "Cleanliness: Satisfied\nSignage: Neutral"


# ── Resuming an interview ────────────────────────────────────────────────────


def test_a_resumed_interview_replays_the_ratings_not_the_json():
    interview_id, project_id = uuid.uuid4(), uuid.uuid4()
    started = datetime.datetime(2026, 11, 10, 10, tzinfo=datetime.UTC)
    item = _item()
    answer = item.encode_answer({ROWS[0].id: "Satisfied", ROWS[1].id: "Neutral"})

    def message(message_id, role, content, **extra) -> MessagePublic:
        return MessagePublic(
            id=uuid.uuid4(),
            message_id=message_id,
            content=content,
            role=role,
            interview_id=interview_id,
            project_id=project_id,
            created_at=started,
            interview_type=InterviewType.DISTRIBUTED,
            section=0,
            **extra,
        )

    grid = message(0, MessageRole.ASSISTANT, "Rate:", main_question=0, survey_item=item)
    probe = message(2, MessageRole.ASSISTANT, "Why?", main_question=0, sub_question=1)

    # The answer mid-history, and as the last message.
    replayed, _ = replay_history(
        [grid, message(1, MessageRole.USER, answer), probe], project_id, interview_id
    )
    assert replayed[1].content == "Cleanliness: Satisfied\nSignage: Neutral"
    replayed, _ = replay_history(
        [grid, message(1, MessageRole.USER, answer)], project_id, interview_id
    )
    assert replayed[-1].content == "Cleanliness: Satisfied\nSignage: Neutral"

    # Only the answer to the grid: a probe's answer after it is left alone.
    replayed, _ = replay_history(
        [
            grid,
            message(1, MessageRole.USER, answer),
            probe,
            message(3, MessageRole.USER, answer),
        ],
        project_id,
        interview_id,
    )
    assert replayed[-1].content == answer
