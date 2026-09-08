"""Tests for browsing the corpus without vectors.

An in-memory SQLite database with a handful of messages in it: no inference
server, no dev corpus, and nothing that depends on what anybody happens to have
collected. The point of most of these is the chunk policy -- browsing expresses
it a second time, in SQL, and these are what keep the two expressions saying the
same thing.
"""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ainterviewer.interview_guides import InterviewGuide
from ainterviewer.interview_guides.survey_items import NumberItem
from ainterviewer.lpm.types import CustomToken
from ainterviewer.types import EmbeddingKind, MessageRole, MessageType
from app.db.repositories.embedding import EmbeddingFilters, EmbeddingRepository
from app.db.tables import Base, InterviewTable, MessageTable
from app.db.types import InterviewType

PROJECT = uuid.uuid4()


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


class Builder:
    """A tiny interview writer, so a test reads as the conversation it sets up."""

    def __init__(self, session: Session, **interview_kwargs):
        self.session = session
        self.interview = InterviewTable(
            id=uuid.uuid4(),
            project_id=PROJECT,
            interview_guide=InterviewGuide(),
            **{"type": InterviewType.DISTRIBUTED, **interview_kwargs},
        )
        session.add(self.interview)
        self.position = 0

    def say(self, role, content, *, section=0, question=0, **kwargs):
        self.position += 1
        message = MessageTable(
            id=uuid.uuid4(),
            message_id=self.position,
            interview_id=self.interview.id,
            project_id=PROJECT,
            role=role,
            content=content,
            section=section,
            main_question=question,
            message_type=kwargs.pop("message_type", MessageType.TEXT),
            **kwargs,
        )
        self.session.add(message)
        return message

    def exchange(self, prompt, answer, **kwargs):
        """An interviewer turn and the respondent turn it drew.

        The survey item belongs to the question, which is exactly the point:
        `Turn.is_free_text` reads it off the question and not off the answer.
        """
        self.say(MessageRole.ASSISTANT, prompt, **kwargs)
        kwargs.pop("survey_item", None)
        return self.say(MessageRole.USER, answer, **kwargs)


def browse(session, kind, **filters):
    return EmbeddingRepository(session).browse(
        project_id=PROJECT,
        kind=kind,
        filters=EmbeddingFilters(**filters),
        limit=100,
        offset=0,
    )


class TestChunkPolicy:
    """Which units exist at all. Every case here is one the library's
    `DefaultChunkPolicy` already decides, restated so the SQL cannot drift."""

    def test_a_free_text_answer_is_a_qa_pair(self, session):
        builder = Builder(session)
        builder.exchange("How is the work going?", "Slowly, but it is going.")
        session.flush()

        assert browse(session, EmbeddingKind.QA_PAIR).total == 1

    def test_a_survey_only_group_is_not(self, session):
        """The answer is a click, not writing. A group of nothing but these is
        survey scaffolding, and embedding it crowds real answers out of the
        neighbour lists."""
        builder = Builder(session)
        builder.exchange("How many hours?", "40", survey_item=NumberItem())
        session.flush()

        assert browse(session, EmbeddingKind.QA_PAIR).total == 0

    def test_the_same_message_is_still_a_message(self, session):
        """The two levels disagree deliberately: `should_embed_message` has no
        survey check, and only `should_embed_question` asks for free text."""
        builder = Builder(session)
        builder.exchange("How many hours?", "40", survey_item=NumberItem())
        session.flush()

        assert browse(session, EmbeddingKind.MESSAGE).total == 1

    def test_free_text_elsewhere_rescues_the_group(self, session):
        """A survey item inside a group that also drew writing is context, not
        a reason to drop the group."""
        builder = Builder(session)
        builder.exchange("How many hours?", "40", survey_item=NumberItem())
        builder.exchange("Why so many?", "The project is behind.")
        session.flush()

        assert browse(session, EmbeddingKind.QA_PAIR).total == 1

    def test_the_interviewer_is_never_a_unit(self, session):
        builder = Builder(session)
        builder.say(MessageRole.ASSISTANT, "How is the work going?")
        session.flush()

        assert browse(session, EmbeddingKind.MESSAGE).total == 0

    def test_a_control_token_is_not_an_answer(self, session):
        """ "Skip this question" is an instruction to the interview loop."""
        builder = Builder(session)
        builder.exchange("How is it going?", CustomToken.skip_question.value)
        session.flush()

        assert browse(session, EmbeddingKind.MESSAGE).total == 0

    def test_an_empty_answer_is_not_an_answer(self, session):
        builder = Builder(session)
        builder.exchange("How is it going?", "   ")
        session.flush()

        assert browse(session, EmbeddingKind.MESSAGE).total == 0

    def test_a_skipped_answer_is_not_an_answer(self, session):
        builder = Builder(session)
        builder.exchange("How is it going?", "Fine", skipped_by_condition=True)
        session.flush()

        assert browse(session, EmbeddingKind.MESSAGE).total == 0

    def test_an_answer_before_the_first_question_is_not_a_unit(self, session):
        """A message with no guide coordinates belongs to no question group."""
        builder = Builder(session)
        builder.say(MessageRole.USER, "Hello?", section=None, question=None)
        session.flush()

        assert browse(session, EmbeddingKind.MESSAGE).total == 0


class TestKeyword:
    def test_narrows_to_matching_messages(self, session):
        builder = Builder(session)
        builder.exchange("And the funding?", "The funding ran out.")
        builder.exchange("And the team?", "Everyone stayed.")
        session.flush()

        assert browse(session, EmbeddingKind.MESSAGE, keyword="funding").total == 1

    def test_is_case_insensitive_by_default(self, session):
        builder = Builder(session)
        builder.exchange("And the funding?", "The Funding ran out.")
        session.flush()

        assert browse(session, EmbeddingKind.MESSAGE, keyword="FUNDING").total == 1

    def test_matches_the_answer_not_the_question(self, session):
        """A chunk restates the question it answers. Matching the rendered chunk
        would let a word the interviewer said count as one the respondent did."""
        builder = Builder(session)
        builder.exchange("What about the funding?", "It was fine.")
        session.flush()

        assert browse(session, EmbeddingKind.MESSAGE, keyword="funding").total == 0

    def test_a_percent_sign_is_a_percent_sign(self, session):
        """Unescaped, `%` is the LIKE wildcard and matches every row."""
        builder = Builder(session)
        builder.exchange("How much?", "About 100% of it.")
        builder.exchange("And the rest?", "Nothing left.")
        session.flush()

        assert browse(session, EmbeddingKind.MESSAGE, keyword="100%").total == 1

    def test_an_underscore_is_an_underscore(self, session):
        builder = Builder(session)
        builder.exchange("Which file?", "The one called a_b.")
        builder.exchange("Any other?", "The one called axb.")
        session.flush()

        assert browse(session, EmbeddingKind.MESSAGE, keyword="a_b").total == 1

    def test_exact_match_is_the_whole_message(self, session):
        builder = Builder(session)
        builder.exchange("Agree?", "Yes")
        builder.exchange("Really?", "Yes, entirely.")
        session.flush()

        page = browse(session, EmbeddingKind.MESSAGE, keyword="yes", keyword_exact=True)

        assert page.total == 1

    def test_lifts_to_the_group_for_qa_pairs(self, session):
        """A pair matches when a message inside it does."""
        builder = Builder(session)
        builder.exchange("How is it going?", "Slowly.")
        builder.exchange("Why?", "The funding ran out.")
        session.flush()

        assert browse(session, EmbeddingKind.QA_PAIR, keyword="funding").total == 1


class TestFilters:
    def test_questions_narrow_to_the_guide_coordinates(self, session):
        builder = Builder(session)
        builder.exchange("First?", "One.", section=0, question=0)
        builder.exchange("Second?", "Two.", section=0, question=1)
        session.flush()

        page = browse(session, EmbeddingKind.QA_PAIR, questions=[(0, 1)])

        assert page.total == 1
        assert page.units[0].main_question == 1

    def test_test_runs_are_left_out_by_default(self, session):
        builder = Builder(session, type=InterviewType.SYNTHETIC_TEST)
        builder.exchange("How is it going?", "Slowly.")
        session.flush()

        assert browse(session, EmbeddingKind.MESSAGE).total == 0
        assert browse(session, EmbeddingKind.MESSAGE, include_synthetic=True).total == 1

    def test_language_is_the_interview_s(self, session):
        """Messages carry no language of their own; the interview does."""
        danish = Builder(session, language="DA")
        danish.exchange("Hvordan går det?", "Langsomt.")
        english = Builder(session, language="EN")
        english.exchange("How is it going?", "Slowly.")
        session.flush()

        page = browse(session, EmbeddingKind.MESSAGE, languages=["DA"])

        assert page.total == 1
        assert page.units[0].language == "DA"


class TestUnits:
    def test_an_unembedded_unit_says_so(self, session):
        """Nothing here has a vector, so nothing can be asked what it is near."""
        builder = Builder(session)
        builder.exchange("How is it going?", "Slowly.")
        session.flush()

        unit = browse(session, EmbeddingKind.QA_PAIR).units[0]

        assert unit.embedded is False
        assert unit.text is None

    def test_an_unembedded_unit_keeps_its_id(self, session):
        """Selection and paging need a row to still be the same row on the next
        request, which a random id would not survive."""
        builder = Builder(session)
        builder.exchange("How is it going?", "Slowly.")
        session.flush()

        first = browse(session, EmbeddingKind.QA_PAIR).units[0]
        second = browse(session, EmbeddingKind.QA_PAIR).units[0]

        assert first.id == second.id

    def test_units_read_as_the_conversation(self, session):
        """Browsing renders through the same `turns_for` the search path uses,
        rather than a second rendering that could drift from it."""
        builder = Builder(session)
        builder.exchange("How is it going?", "Slowly, but it is going.")
        session.flush()

        page = browse(session, EmbeddingKind.QA_PAIR)
        turns = EmbeddingRepository(session).turns_for(page.units)

        assert [turn.text for turn in turns[page.units[0].id]] == [
            "How is it going?",
            "Slowly, but it is going.",
        ]

    def test_paging_walks_a_declared_order(self, session):
        builder = Builder(session)
        for index in range(5):
            builder.exchange(f"Q{index}?", f"A{index}", question=index)
        session.flush()

        repository = EmbeddingRepository(session)
        page = repository.browse(
            project_id=PROJECT,
            kind=EmbeddingKind.QA_PAIR,
            filters=EmbeddingFilters(),
            limit=2,
            offset=2,
        )

        assert page.total == 5
        assert [unit.main_question for unit in page.units] == [2, 3]


class TestMessageTurns:
    """What one message chunk renders as.

    A question group with several probes produces one MESSAGE chunk per probe,
    so what each of them shows decides whether a list of them reads as distinct
    answers or as the same conversation written out N times.
    """

    def test_a_message_is_its_own_probe_and_answer(self, session):
        builder = Builder(session)
        builder.exchange("How is it going?", "Slowly.")
        builder.exchange("Why is that?", "The funding ran out.")
        session.flush()

        repository = EmbeddingRepository(session)
        page = repository.browse(
            project_id=PROJECT,
            kind=EmbeddingKind.MESSAGE,
            filters=EmbeddingFilters(),
            limit=100,
            offset=0,
        )
        turns = repository.turns_for(page.units)

        assert [[turn.text for turn in turns[unit.id]] for unit in page.units] == [
            ["How is it going?", "Slowly."],
            ["Why is that?", "The funding ran out."],
        ]

    def test_chunks_of_one_group_do_not_repeat_each_other(self, session):
        """The regression this replaced: each chunk was the whole group up to
        its own message, so three probes rendered as three growing prefixes of
        one conversation."""
        builder = Builder(session)
        builder.exchange("First?", "One.")
        builder.exchange("Second?", "Two.")
        builder.exchange("Third?", "Three.")
        session.flush()

        repository = EmbeddingRepository(session)
        page = repository.browse(
            project_id=PROJECT,
            kind=EmbeddingKind.MESSAGE,
            filters=EmbeddingFilters(),
            limit=100,
            offset=0,
        )
        turns = repository.turns_for(page.units)

        rendered = [
            text for unit in page.units for text in (t.text for t in turns[unit.id])
        ]

        assert len(rendered) == len(set(rendered))

    def test_a_message_with_no_probe_before_it_stands_alone(self, session):
        """A respondent writing twice in a row must not borrow the question of
        the message above it."""
        builder = Builder(session)
        builder.exchange("How is it going?", "Slowly.")
        builder.say(MessageRole.USER, "And getting slower.")
        session.flush()

        repository = EmbeddingRepository(session)
        page = repository.browse(
            project_id=PROJECT,
            kind=EmbeddingKind.MESSAGE,
            filters=EmbeddingFilters(),
            limit=100,
            offset=0,
        )
        turns = repository.turns_for(page.units)

        assert [turn.text for turn in turns[page.units[-1].id]] == [
            "And getting slower."
        ]

    def test_the_answer_is_the_turn_marked_as_the_match(self, session):
        """`match` is what a client rings while searching, so it has to be the
        answer rather than the probe."""
        builder = Builder(session)
        builder.exchange("How is it going?", "Slowly.")
        session.flush()

        repository = EmbeddingRepository(session)
        page = repository.browse(
            project_id=PROJECT,
            kind=EmbeddingKind.MESSAGE,
            filters=EmbeddingFilters(),
            limit=100,
            offset=0,
        )
        turns = repository.turns_for(page.units)
        matched = [turn for turn in turns[page.units[0].id] if turn.match]

        assert [turn.text for turn in matched] == ["Slowly."]
