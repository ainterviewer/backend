"""Tests for browsing the corpus without vectors.

An in-memory SQLite database with a handful of messages in it: no inference
server, no dev corpus, and nothing that depends on what anybody happens to have
collected. The point of most of these is the chunk policy -- browsing expresses
it a second time, in SQL, and these are what keep the two expressions saying the
same thing.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from ainterviewer.interview_guides import InterviewGuide
from ainterviewer.interview_guides.survey_items import NumberItem
from ainterviewer.lpm.types import CustomToken
from ainterviewer.types import EmbeddingKind, MessageRole, MessageType
from app.db.keyword_query import KeywordQueryError
from app.db.models import EmbeddingSearchHit
from app.db.regexp import register_regexp
from app.db.repositories.embedding import EmbeddingFilters, EmbeddingRepository
from app.db.tables import Base, InterviewTable, MessageTable
from app.db.types import InterviewType

PROJECT = uuid.uuid4()


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    # Keyword search compiles to a regular expression, and SQLite has no
    # REGEXP of its own -- the app registers one per connection and so must
    # anything that queries with it.
    register_regexp(engine)
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

    def test_matches_words_not_substrings(self, session):
        """`kat` is not `katalog`. The forgiving substring match is still there
        behind a `*`, but it has to be asked for -- searching for a short word
        and getting every longer word containing it is the more common
        surprise."""
        builder = Builder(session)
        builder.exchange("And then?", "We read the catalogue.")
        session.flush()

        assert browse(session, EmbeddingKind.MESSAGE, keyword="cat").total == 0
        assert browse(session, EmbeddingKind.MESSAGE, keyword="cat*").total == 1
        assert browse(session, EmbeddingKind.MESSAGE, keyword="catalogue").total == 1

    def test_punctuation_does_not_break_a_word_match(self, session):
        """A word at the end of a sentence is still that word."""
        builder = Builder(session)
        builder.exchange("Agree?", "Yes, entirely.")
        session.flush()

        assert browse(session, EmbeddingKind.MESSAGE, keyword="yes").total == 1

    def test_lifts_to_the_group_for_qa_pairs(self, session):
        """A pair matches when a message inside it does."""
        builder = Builder(session)
        builder.exchange("How is it going?", "Slowly.")
        builder.exchange("Why?", "The funding ran out.")
        session.flush()

        assert browse(session, EmbeddingKind.QA_PAIR, keyword="funding").total == 1


class TestBooleanKeywords:
    """The keyword box is a query language, not a string to look for.

    The grammar itself is tested in `test_keyword_query.py`; these check that it
    reaches the database and means there what it means there."""

    @pytest.fixture
    def corpus(self, session):
        builder = Builder(session)
        builder.exchange("Pets?", "We have a dog.")
        builder.exchange("Any more?", "A cat, and a puppy.")
        builder.exchange("Anything else?", "Just the goldfish.")
        session.flush()
        return session

    def count(self, session, keyword):
        return browse(session, EmbeddingKind.MESSAGE, keyword=keyword).total

    def test_or_widens(self, corpus):
        assert self.count(corpus, "dog") == 1
        assert self.count(corpus, "dog OR cat") == 2

    def test_adjacent_terms_mean_and(self, corpus):
        assert self.count(corpus, "cat puppy") == 1
        assert self.count(corpus, "dog puppy") == 0

    def test_not_excludes(self, corpus):
        assert self.count(corpus, "cat AND NOT puppy") == 0
        assert self.count(corpus, "cat -goldfish") == 1

    def test_brackets_group(self, corpus):
        assert self.count(corpus, "(dog OR cat) AND puppy") == 1
        assert self.count(corpus, "dog OR (cat AND puppy)") == 2

    def test_a_phrase_is_its_words_in_order(self, session):
        builder = Builder(session)
        builder.exchange("Who?", "My neighbour said so.")
        builder.exchange("Who else?", "A neighbour of my mother.")
        session.flush()

        assert self.count(session, '"my neighbour"') == 1
        assert self.count(session, "my neighbour") == 2

    def test_a_phrase_matches_across_a_line_break(self, session):
        builder = Builder(session)
        builder.exchange("Who?", "My\n   neighbour said so.")
        session.flush()

        assert self.count(session, '"my neighbour"') == 1

    def test_operators_are_case_insensitive(self, corpus):
        assert self.count(corpus, "dog or cat") == 2

    def test_a_quoted_operator_is_a_word(self, session):
        builder = Builder(session)
        builder.exchange("And?", "Or so they said.")
        session.flush()

        assert self.count(session, '"or"') == 1

    def test_a_malformed_query_raises_rather_than_matching(self, session):
        with pytest.raises(KeywordQueryError):
            self.count(session, "(dog OR cat")


class TestKeywordScope:
    """Which side of the exchange a term is looked for in.

    The default is the answer, and deliberately so: a chunk restates the
    question it answers, so counting the interviewer's words by default would
    let the guide's own phrasing read as a finding."""

    @pytest.fixture
    def corpus(self, session):
        builder = Builder(session)
        builder.exchange("Fortæl om din stress", "Jeg var meget træt")
        builder.exchange("Og dit arbejde?", "Det gik fint")
        session.flush()
        return session

    def count(self, session, keyword, scope="answer", kind=EmbeddingKind.MESSAGE):
        return browse(session, kind, keyword=keyword, keyword_scope=scope).total

    def test_answers_only_by_default(self, corpus):
        assert self.count(corpus, "stress") == 0
        assert self.count(corpus, "træt") == 1

    def test_questions_can_be_asked_for(self, corpus):
        assert self.count(corpus, "stress", "question") == 1
        assert self.count(corpus, "træt", "question") == 0

    def test_both_takes_either_side(self, corpus):
        assert self.count(corpus, "stress", "both") == 1
        assert self.count(corpus, "træt", "both") == 1

    def test_a_question_match_returns_the_answer_that_followed(self, corpus):
        """A message chunk is a respondent message. The question matched, so
        the answer to it is the finding, rendered under the question that drew
        it."""
        page = browse(
            corpus,
            EmbeddingKind.MESSAGE,
            keyword="stress",
            keyword_scope="question",
        )
        turns = EmbeddingRepository(corpus).turns_for(
            page.units, EmbeddingFilters(keyword="stress", keyword_scope="question")
        )

        assert page.total == 1
        assert [turn.text for turn in turns[page.units[0].id]] == [
            "Fortæl om din stress",
            "Jeg var meget træt",
        ]

    def test_a_prefix_overrides_the_scope(self, corpus):
        # The whole point of the prefixes: a scope no single toggle can express.
        assert self.count(corpus, "q:stress a:træt") == 1
        assert self.count(corpus, "q:arbejde a:træt") == 0

    def test_a_prefix_works_against_the_other_default(self, corpus):
        assert self.count(corpus, "a:træt", "question") == 1

    def test_a_quoted_operator_survives_a_prefix(self, session):
        builder = Builder(session)
        builder.exchange("Or what?", "Ja")
        session.flush()

        # `q:or` is the word "or" in the question, not a dangling operator.
        assert self.count(session, "q:or") == 1

    def test_a_respondent_writing_twice_answers_no_new_question(self, session):
        """The row before a respondent turn is only a question when an
        interviewer said it."""
        builder = Builder(session)
        builder.exchange("Fortæl om stress", "Jeg var træt")
        builder.say(MessageRole.USER, "og desuden urolig")
        session.flush()

        # Both answers are in scope; only the first has a question above it.
        assert self.count(session, "stress", "question") == 1


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


class TestMatchSpans:
    """Where a turn says what was searched for.

    Reported by the server so that the thing which decided a row is on screen is
    the thing that marks it -- the scope in particular is only knowable here."""

    @pytest.fixture
    def corpus(self, session):
        builder = Builder(session)
        builder.exchange("Fortæl om din stress", "Jeg var meget træt af stress")
        session.flush()
        return session

    def spans(self, session, keyword, scope="answer"):
        filters = EmbeddingFilters(keyword=keyword, keyword_scope=scope)
        page = browse(
            session, EmbeddingKind.MESSAGE, keyword=keyword, keyword_scope=scope
        )
        turns = EmbeddingRepository(session).turns_for(page.units, filters)
        return [
            [(turn.text[start:end]) for start, end in turn.matches]
            for turn in turns[page.units[0].id]
        ]

    def test_marks_the_answer_it_matched(self, corpus):
        assert self.spans(corpus, "træt") == [[], ["træt"]]

    def test_leaves_the_question_alone_when_scoped_to_answers(self, corpus):
        """The same word is in the question, and under this scope it is not a
        match -- marking it would claim the search found something it did not."""
        assert self.spans(corpus, "stress") == [[], ["stress"]]

    def test_marks_the_question_when_asked_to(self, corpus):
        assert self.spans(corpus, "stress", "question") == [["stress"], []]

    def test_marks_both_sides_when_scoped_to_both(self, corpus):
        assert self.spans(corpus, "stress", "both") == [["stress"], ["stress"]]

    def test_a_prefix_places_the_mark(self, corpus):
        assert self.spans(corpus, "q:stress") == [["stress"], []]

    def test_marks_whole_words_for_a_wildcard(self, session):
        """The database matched the prefix; marking six letters and leaving the
        rest of the word dark would point at a fragment nobody searched for."""
        builder = Builder(session)
        builder.exchange("Og?", "på arbejdspladsen")
        session.flush()

        assert self.spans(session, "arbejd*") == [[], ["arbejdspladsen"]]

    def test_excluded_terms_are_not_marked(self, session):
        """The answer says "børn" and not "skole", so it is a hit. "skole" is on
        screen in the question above it, and marking it would point at the
        reason a chunk was *excluded*, inside one that was not."""
        builder = Builder(session)
        builder.exchange("Noget om skole?", "vores børn")
        session.flush()

        assert self.spans(session, "børn -skole") == [[], ["børn"]]

    def test_no_keyword_marks_nothing(self, session):
        builder = Builder(session)
        builder.exchange("Og?", "et svar")
        session.flush()

        page = browse(session, EmbeddingKind.MESSAGE)
        turns = EmbeddingRepository(session).turns_for(page.units)

        assert all(turn.matches == [] for turn in turns[page.units[0].id])


class TestExcludedSpans:
    """Where a term the query excluded shows up on the card anyway.

    It can, in two ways, and both are worth seeing rather than hiding: in text
    the scope never searched, and in a sibling turn of a grouped chunk, because
    the condition is checked per message and then lifted to the group."""

    def spans(self, session, keyword, kind=EmbeddingKind.MESSAGE, scope="answer"):
        filters = EmbeddingFilters(keyword=keyword, keyword_scope=scope)
        page = browse(session, kind, keyword=keyword, keyword_scope=scope)
        turns = EmbeddingRepository(session).turns_for(page.units, filters)
        return [
            (
                [turn.text[a:b] for a, b in turn.matches],
                [turn.text[a:b] for a, b in turn.excluded],
            )
            for turn in turns[page.units[0].id]
        ]

    def test_marks_an_excluded_word_in_unsearched_text(self, session):
        """The scope is Answers, so the question was never searched -- but the
        word is on screen, and it is one the reader asked not to see."""
        builder = Builder(session)
        builder.exchange("Noget om skole?", "vores børn")
        session.flush()

        assert self.spans(session, "børn -skole") == [([], ["skole"]), (["børn"], [])]

    def test_marks_an_excluded_word_in_a_sibling_turn(self, session):
        """A pair qualifies when one message says børn and not skole. Another
        answer in the same pair is free to say skole, and does."""
        builder = Builder(session)
        builder.exchange("Og?", "vores børn")
        builder.exchange("Og videre?", "meget skole")
        session.flush()

        marked = self.spans(session, "børn -skole", EmbeddingKind.QA_PAIR)

        assert (["børn"], []) in marked
        assert ([], ["skole"]) in marked

    def test_a_match_wins_over_an_exclusion(self, session):
        """No character is marked twice, whatever a contradictory query asks."""
        builder = Builder(session)
        builder.exchange("Og?", "vores børn")
        session.flush()

        for matches, excluded in self.spans(session, "børn OR -børn"):
            assert not (matches and excluded)

    def test_nothing_excluded_without_a_negation(self, session):
        builder = Builder(session)
        builder.exchange("Og?", "vores børn")
        session.flush()

        assert all(excluded == [] for _, excluded in self.spans(session, "børn"))


class TestResponseModel:
    """Browsed units have to survive the response model, not just the query.

    The rest of this file calls the repository directly, which is why an
    un-embedded unit's id being rejected by `EmbeddingSearchHit` went unseen:
    every test passed and every request 500'd."""

    def test_an_unembedded_unit_serialises(self, session):
        """Its id is a `uuid5` of its coordinates -- deterministic, so version 5.
        The model asked for version 4 and rejected exactly the rows browsing
        exists to serve."""
        builder = Builder(session)
        builder.exchange("Og?", "et svar")
        session.flush()

        page = browse(session, EmbeddingKind.MESSAGE)
        turns = EmbeddingRepository(session).turns_for(page.units)
        unit = page.units[0]

        hit = EmbeddingSearchHit.from_hit(unit, None, turns.get(unit.id))

        assert hit.id == unit.id
        assert hit.embedded is False
        assert hit.score is None

    def test_an_embedded_unit_still_serialises(self, session):
        """The other half of the same contract: a real embedding row keeps its
        own version-4 id."""
        builder = Builder(session)
        builder.exchange("Og?", "et svar")
        session.flush()

        page = browse(session, EmbeddingKind.MESSAGE)
        unit = page.units[0]

        assert EmbeddingSearchHit.from_hit(unit, 0.5, None).score == 0.5


class TestTranscript:
    """The whole interview behind a hit, as the modal reads it.

    `turns_for` renders a chunk; this renders everything, which is the whole
    reason the two are separate methods -- what a transcript must *not* do is
    drop the turns a chunk left out.
    """

    def transcript(self, session, interview_id, **kwargs):
        return EmbeddingRepository(session).transcript(
            project_id=PROJECT, interview_id=interview_id, **kwargs
        )

    def test_it_keeps_every_turn_not_only_the_chunk(self, session):
        builder = Builder(session)
        builder.exchange("Hvordan?", "godt", question=0)
        builder.exchange("Og arbejde?", "travlt", question=1)
        session.flush()

        turns = self.transcript(session, builder.interview.id)

        assert [turn.text for turn in turns] == [
            "Hvordan?",
            "godt",
            "Og arbejde?",
            "travlt",
        ]

    def test_it_carries_the_guide_coordinates(self, session):
        """What the modal scrolls by: the card knows its own section and
        question, and matching them against these is how it finds the place."""
        builder = Builder(session)
        builder.exchange("Hvordan?", "godt", section=1, question=2)
        session.flush()

        turns = self.transcript(session, builder.interview.id)

        assert all(turn.section == 1 and turn.main_question == 2 for turn in turns)

    def test_a_turn_before_the_first_question_survives(self, session):
        """`turns_for` drops these -- a chunk is a question group and this one
        belongs to none. A transcript is the conversation, so it keeps them."""
        builder = Builder(session)
        builder.say(MessageRole.ASSISTANT, "Velkommen.", section=None, question=None)
        builder.exchange("Hvordan?", "godt")
        session.flush()

        turns = self.transcript(session, builder.interview.id)

        assert turns[0].text == "Velkommen."
        assert turns[0].section is None

    def test_skipped_and_token_turns_are_kept(self, session):
        """Unlike a chunk, which drops both. A question the guide routed around
        and the token that closed a section are part of how the interview went,
        and the transcript page has always shown them."""
        builder = Builder(session)
        builder.say(MessageRole.ASSISTANT, "Sprunget over", skipped_by_condition=True)
        builder.say(MessageRole.ASSISTANT, next(iter(CustomToken)))
        builder.exchange("Hvordan?", "godt")
        session.flush()

        turns = self.transcript(session, builder.interview.id)

        assert [turn.text for turn in turns] == [
            "Sprunget over",
            next(iter(CustomToken)),
            "Hvordan?",
            "godt",
        ]
        assert turns[0].skipped is True
        assert turns[2].skipped is False

    def test_a_survey_item_moves_onto_the_answer(self, session):
        """Stored on the question, read on the answer: what a reader judges is
        the option *and* the options it was chosen from."""
        item = NumberItem(min=1, max=5)
        builder = Builder(session)
        builder.exchange("Hvor ofte?", "3", survey_item=item)
        session.flush()

        question, answer = self.transcript(session, builder.interview.id)

        assert question.survey_item is None
        assert answer.survey_item == item
        assert answer.survey_label == item.type

    def test_the_keyword_is_marked_in_the_answers(self, session):
        builder = Builder(session)
        builder.exchange("Er du stresset?", "ja jeg er stresset")
        session.flush()

        turns = self.transcript(session, builder.interview.id, keyword="stresset")

        question, answer = turns
        # Scope is "answer" by default, so the interviewer saying the word is
        # not a match -- the same rule the mosaic marks by.
        assert question.matches == []
        assert answer.matches == [(10, 18)]

    def test_the_scope_reaches_the_questions(self, session):
        builder = Builder(session)
        builder.exchange("Er du stresset?", "ja")
        session.flush()

        turns = self.transcript(
            session, builder.interview.id, keyword="stresset", keyword_scope="question"
        )

        assert turns[0].matches == [(6, 14)]

    def test_an_excluded_word_is_reported_separately(self, session):
        builder = Builder(session)
        builder.exchange("Og?", "børn men ikke arbejde")
        session.flush()

        turns = self.transcript(session, builder.interview.id, keyword="børn -arbejde")

        answer = turns[1]
        assert answer.matches == [(0, 4)]
        assert answer.excluded == [(14, 21)]

    def test_another_project_s_interview_is_absent_not_forbidden(self, session):
        """The route is authorised on the project, so an interview belonging
        elsewhere must not be readable through it."""
        builder = Builder(session)
        builder.exchange("Hvordan?", "godt")
        session.flush()

        with pytest.raises(NoResultFound):
            EmbeddingRepository(session).transcript(
                project_id=uuid.uuid4(), interview_id=builder.interview.id
            )

    def test_a_query_that_cannot_be_read_is_refused(self, session):
        builder = Builder(session)
        builder.exchange("Hvordan?", "godt")
        session.flush()

        with pytest.raises(KeywordQueryError):
            self.transcript(session, builder.interview.id, keyword="(stress")


class TestMarkupIsNotProse:
    """The SQL half of the rule `TestMarkup` pins in Python.

    Two expressions of one idea -- `match_spans` drops a span inside a tag, and
    the keyword condition strips tags before matching -- and they have to agree
    or a search returns a row with nothing in it to see.
    """

    def question(self, session, text):
        builder = Builder(session)
        builder.exchange(text, "et svar")
        session.flush()
        return builder

    def hits(self, session, keyword, scope="question"):
        return browse(
            session,
            EmbeddingKind.QA_PAIR,
            keyword=keyword,
            keyword_scope=scope,
        ).units

    def test_a_word_inside_an_href_does_not_select_the_row(self, session):
        self.question(
            session,
            'Læs mere <a href="https://ku.dk/Stressand-x.aspx">her</a>?',
        )

        assert self.hits(session, "stress*") == []

    def test_the_prose_in_the_same_question_still_selects_it(self, session):
        self.question(
            session,
            'Er du stresset? <a href="https://ku.dk/Stressand-x.aspx">her</a>',
        )

        assert len(self.hits(session, "stress*")) == 1

    def test_a_tag_name_is_not_a_word(self, session):
        # `<u>` is in this project's own guide, and a boundary-matched `u`
        # finds it: `<` and `>` are both non-word characters.
        self.question(session, "Hvor mange timer <u>i gennemsnit</u>?")

        assert self.hits(session, "u") == []

    def test_a_respondent_writing_a_tag_is_writing_text(self, session):
        """Answers are never rendered as markup, so they are not stripped."""
        builder = Builder(session)
        builder.exchange("Og?", "jeg skrev <b>fed</b> tekst")
        session.flush()

        assert len(self.hits(session, "b", scope="answer")) == 1


class TestInterviewCounts:
    """How many interviews a result set is drawn from, and which one a card is.

    A count of chunks says how much there is to read; it says nothing about
    whether it came from forty people or from one who talked a lot, which is
    the difference between a finding and an anecdote.
    """

    def test_the_count_is_of_interviews_not_chunks(self, session):
        first = Builder(session)
        first.exchange("How is it going?", "Slowly.", question=0)
        first.exchange("And now?", "Still slowly.", question=1)
        Builder(session).exchange("How is it going?", "Fine.", question=0)
        session.flush()

        page = browse(session, EmbeddingKind.QA_PAIR)

        assert page.total == 3
        assert page.interviews == 2

    def test_it_counts_everything_matched_not_the_page(self, session):
        for _ in range(4):
            Builder(session).exchange("How is it going?", "Slowly.")
        session.flush()

        page = EmbeddingRepository(session).browse(
            project_id=PROJECT,
            kind=EmbeddingKind.QA_PAIR,
            filters=EmbeddingFilters(),
            limit=1,
            offset=0,
        )

        assert len(page.units) == 1
        assert page.interviews == 4

    def test_a_filter_that_drops_an_interview_drops_it_from_the_count(self, session):
        Builder(session).exchange("How is it going?", "Slowly.")
        Builder(session).exchange("How is it going?", "The funding ran out.")
        session.flush()

        assert browse(session, EmbeddingKind.QA_PAIR, keyword="funding").interviews == 1

    def test_interviews_are_numbered_in_the_order_they_started(self, session):
        first = Builder(session, created_at=datetime(2026, 1, 1, tzinfo=UTC))
        second = Builder(session, created_at=datetime(2026, 2, 1, tzinfo=UTC))
        session.flush()

        numbers = EmbeddingRepository(session).interview_numbers(PROJECT)

        assert numbers[first.interview.id] == 1
        assert numbers[second.interview.id] == 2

    def test_the_number_does_not_move_when_a_filter_does(self, session):
        """The whole point of the number: it identifies an interview across
        searches, so it cannot be a rank within the current result set."""
        Builder(session, created_at=datetime(2026, 1, 1, tzinfo=UTC)).exchange(
            "Q?", "Slowly."
        )
        second = Builder(session, created_at=datetime(2026, 2, 1, tzinfo=UTC))
        second.exchange("Q?", "The funding ran out.")
        session.flush()

        numbers = EmbeddingRepository(session).interview_numbers(PROJECT)
        matched = browse(session, EmbeddingKind.QA_PAIR, keyword="funding").units

        assert [numbers[unit.interview_id] for unit in matched] == [2]

    def test_an_interview_no_filter_admits_is_still_numbered(self, session):
        """Numbering runs over the project, not over what a request left: a
        test run being hidden must not renumber the interviews after it."""
        Builder(
            session,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            type=InterviewType.SYNTHETIC_TEST,
        )
        real = Builder(session, created_at=datetime(2026, 2, 1, tzinfo=UTC))
        session.flush()

        assert (
            EmbeddingRepository(session).interview_numbers(PROJECT)[real.interview.id]
            == 2
        )


class TestTurnCoordinates:
    """Where each turn of a chunk sits in the guide.

    A card numbers its messages the way the transcript does, and inside one
    question group the probes are what differ -- so the number has to come from
    the turn rather than from the chunk it belongs to.
    """

    def test_every_turn_carries_its_place_in_the_guide(self, session):
        builder = Builder(session)
        builder.exchange("How is it going?", "Slowly.", section=2, question=1)
        session.flush()

        page = browse(session, EmbeddingKind.QA_PAIR)
        turns = EmbeddingRepository(session).turns_for(page.units)[page.units[0].id]

        assert [(turn.section, turn.main_question) for turn in turns] == [
            (2, 1),
            (2, 1),
        ]

    def test_probes_inside_one_group_are_numbered_apart(self, session):
        builder = Builder(session)
        builder.exchange("How is it going?", "Slowly.", sub_question=1)
        builder.exchange("Why is that?", "The funding ran out.", sub_question=2)
        session.flush()

        page = browse(session, EmbeddingKind.QA_PAIR)
        turns = EmbeddingRepository(session).turns_for(page.units)[page.units[0].id]

        assert [turn.sub_question for turn in turns] == [1, 1, 2, 2]

    def test_a_turn_the_guide_never_numbered_carries_nothing(self, session):
        builder = Builder(session)
        builder.exchange("How is it going?", "Slowly.")
        session.flush()

        page = browse(session, EmbeddingKind.QA_PAIR)
        turns = EmbeddingRepository(session).turns_for(page.units)[page.units[0].id]

        assert [turn.sub_question for turn in turns] == [None, None]


class TestBrowseOrder:
    """Which interview a reader meets first.

    A researcher reads the top of a list more carefully than the bottom, so a
    fixed order quietly decides whose answers get the close reading. These are
    about that, not about the SQL.
    """

    @pytest.fixture
    def corpus(self, session):
        """Five interviews, started a day apart, one Q&A pair each."""
        interviews = []
        for day in range(1, 6):
            builder = Builder(session, created_at=datetime(2026, 1, day, tzinfo=UTC))
            builder.exchange("How is it going?", f"Answer {day}")
            interviews.append(builder.interview.id)
        session.flush()
        return interviews

    def order(self, session, order, seed=""):
        page = EmbeddingRepository(session).browse(
            project_id=PROJECT,
            kind=EmbeddingKind.QA_PAIR,
            filters=EmbeddingFilters(),
            limit=100,
            offset=0,
            order=order,
            seed=seed,
        )
        return [unit.interview_id for unit in page.units]

    def test_ascending_is_the_order_they_were_started(self, session, corpus):
        assert self.order(session, "interview_asc") == corpus

    def test_descending_is_the_reverse(self, session, corpus):
        assert self.order(session, "interview_desc") == list(reversed(corpus))

    def test_a_seed_is_an_order(self, session, corpus):
        """The same seed twice is the same list twice, which is what paging
        rests on: page two has to continue page one, not a new shuffle."""
        assert self.order(session, "random", "abc") == self.order(
            session, "random", "abc"
        )

    def test_a_different_seed_is_a_different_order(self, session, corpus):
        """Several seeds rather than two, because five interviews can land in
        the same order under two seeds by chance -- one time in 120, which is
        often enough to fail a suite. The claim is only that the seed is read
        at all: a shuffle that ignored it would give one order for all four."""
        orders = {
            tuple(str(interview) for interview in self.order(session, "random", seed))
            for seed in ("a", "b", "c", "d")
        }

        assert len(orders) > 1

    def test_a_shuffle_is_still_the_whole_corpus(self, session, corpus):
        assert sorted(map(str, self.order(session, "random", "abc"))) == sorted(
            map(str, corpus)
        )

    def test_paging_a_shuffle_does_not_repeat_or_skip(self, session, corpus):
        repository = EmbeddingRepository(session)
        seen = []
        for offset in (0, 2, 4):
            page = repository.browse(
                project_id=PROJECT,
                kind=EmbeddingKind.QA_PAIR,
                filters=EmbeddingFilters(),
                limit=2,
                offset=offset,
                order="random",
                seed="abc",
            )
            seen.extend(unit.interview_id for unit in page.units)

        assert seen == self.order(session, "random", "abc")

    def test_an_interview_s_chunks_stay_together_when_shuffled(self, session):
        """Interviews move; a conversation does not come apart. Reading one
        interview's answers in sequence is what the card numbers are for."""
        for day in range(1, 4):
            builder = Builder(session, created_at=datetime(2026, 1, day, tzinfo=UTC))
            for question in range(3):
                builder.exchange("Q?", f"A{question}", question=question)
        session.flush()

        order = self.order(session, "random", "abc")
        runs = [
            interview
            for index, interview in enumerate(order)
            if index == 0 or interview != order[index - 1]
        ]

        assert len(order) == 9
        assert len(runs) == 3

    def test_the_guide_still_orders_what_is_inside_one(self, session):
        builder = Builder(session, created_at=datetime(2026, 1, 1, tzinfo=UTC))
        for question in (2, 0, 1):
            builder.exchange("Q?", f"A{question}", question=question)
        session.flush()

        page = EmbeddingRepository(session).browse(
            project_id=PROJECT,
            kind=EmbeddingKind.QA_PAIR,
            filters=EmbeddingFilters(),
            limit=100,
            offset=0,
            order="random",
            seed="abc",
        )

        assert [unit.main_question for unit in page.units] == [0, 1, 2]
