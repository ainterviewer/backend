"""Tests for filtering a corpus by what its respondents answered to survey items.

The filter is a *cohort* filter: it selects interviews by their survey answers
and then keeps every chunk of them, so most of these assert on what browsing
returns rather than on the answers themselves.

The same in-memory SQLite setup as `test_browse.py`. What is worth pinning here
is mostly agreement rather than SQL: an option means the same choice in every
language it was asked in, a checkbox answer holds several values at once, and a
value nobody gave returns nothing rather than everything.
"""

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ainterviewer.interview_guides import InterviewGuide, Question, QuestionSection
from ainterviewer.interview_guides.survey_items import (
    CheckboxItem,
    DateItem,
    NumberItem,
    RadioItem,
)
from ainterviewer.lpm.types import CustomToken
from ainterviewer.types import EmbeddingKind, MessageRole, MessageType
from app.api.dashboard.analysis.embeddings import _parse_survey, survey_facets
from app.db.regexp import register_regexp
from app.db.repositories.embedding import EmbeddingFilters, EmbeddingRepository
from app.db.survey_answers import (
    OptionValue,
    Range,
    SurveyFilter,
    TextValue,
    answer_rows,
    matching_interviews,
    values_of,
)
from app.db.tables import (
    Base,
    InterviewTable,
    MessageTable,
    ProjectLocalizationTable,
)
from app.db.types import InterviewType

PROJECT = uuid.uuid4()

GENDER = RadioItem(options=["Female", "Male", "Non-binary"], with_other=True)
GENDER_DA = RadioItem(options=["Kvinde", "Mand", "Non-binær"], with_other=True)
TRANSPORT = CheckboxItem(options=["Bike", "Bus", "Car"])
AGE = NumberItem()
STARTED = DateItem()


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    register_regexp(engine)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


class Builder:
    """One interview, written as the conversation it was."""

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
        self.say(MessageRole.ASSISTANT, prompt, **kwargs)
        kwargs.pop("survey_item", None)
        return self.say(MessageRole.USER, answer, **kwargs)


def survey(values=None, ranges=None):
    return SurveyFilter(values=values or {}, ranges=ranges or {})


def browse(session, **filters):
    return EmbeddingRepository(session).browse(
        project_id=PROJECT,
        kind=EmbeddingKind.QA_PAIR,
        filters=EmbeddingFilters(**filters),
        limit=100,
        offset=0,
    )


def matching(session, filter_, **kwargs):
    return matching_interviews(session, PROJECT, filter_, **kwargs)


def respondent(session, gender, answer="It has been a long year.", **interview_kwargs):
    """One interview: a survey question, then something the filter can return."""
    builder = Builder(session, **interview_kwargs)
    builder.exchange("What is your gender?", gender, survey_item=GENDER)
    builder.exchange("How has the year been?", answer, section=1, question=0)
    return builder


class TestCohortFiltering:
    def test_a_chosen_option_keeps_only_that_cohort(self, session):
        respondent(session, "Female")
        respondent(session, "Male")
        session.flush()

        assert browse(session).total == 2
        assert browse(session, survey=survey({(0, 0): (OptionValue(1),)})).total == 1

    def test_it_keeps_everything_that_cohort_said(self, session):
        """A cohort filter, not a message filter: the survey answer is what
        selects the interview, and the free text is what comes back."""
        builder = Builder(session)
        builder.exchange("What is your gender?", "Male", survey_item=GENDER)
        builder.exchange("How has the year been?", "Long.", section=1, question=0)
        builder.exchange("And the work?", "Slow.", section=2, question=0)
        session.flush()

        page = browse(session, survey=survey({(0, 0): (OptionValue(1),)}))
        assert page.total == 2

    def test_an_option_means_the_same_choice_in_every_language(self, session):
        """The item is translated, so "Mand" and "Male" are one answer. Matched
        by position for exactly this reason -- by text, the Danish respondent
        would vanish from a filter the reader set in English."""
        respondent(session, "Male")
        danish = Builder(session, language="da")
        danish.exchange("Hvad er dit køn?", "Mand", survey_item=GENDER_DA)
        danish.exchange("Hvordan er året gået?", "Langsomt.", section=1, question=0)
        session.flush()

        assert matching(session, survey({(0, 0): (OptionValue(1),)})) == {
            *(interview.id for interview in session.query(InterviewTable))
        }

    def test_values_within_an_item_are_or_ed(self, session):
        respondent(session, "Female")
        respondent(session, "Male")
        respondent(session, "Non-binary")
        session.flush()

        chosen = survey({(0, 0): (OptionValue(0), OptionValue(2))})
        assert len(matching(session, chosen)) == 2

    def test_two_items_are_and_ed(self, session):
        for gender, age in (("Female", "31"), ("Male", "31"), ("Male", "64")):
            builder = Builder(session)
            builder.exchange("What is your gender?", gender, survey_item=GENDER)
            builder.exchange("Your age?", age, survey_item=AGE, section=1, question=0)
            session.flush()

        both = survey(
            values={(0, 0): (OptionValue(1),)},
            ranges={(1, 0): Range(low="18", high="40")},
        )
        assert len(matching(session, both)) == 1

    def test_a_value_nobody_gave_returns_nothing(self, session):
        """Not everything, which is what an unapplied filter would return. The
        picker shows the count beside each value so this is visible before it
        happens, but the honest answer to "nobody" is still no chunks."""
        respondent(session, "Female")
        session.flush()

        assert browse(session, survey=survey({(0, 0): (OptionValue(2),)})).total == 0

    def test_a_write_in_is_filterable_by_its_text(self, session):
        """It has no position -- that is what makes it a write-in -- so text is
        all there is to match on."""
        respondent(session, "Genderqueer")
        respondent(session, "Female")
        session.flush()

        chosen = survey({(0, 0): (TextValue("genderqueer"),)})
        assert len(matching(session, chosen)) == 1

    def test_a_write_in_matches_however_it_was_typed(self, session):
        respondent(session, "  GenderQueer ")
        session.flush()

        assert (
            len(matching(session, survey({(0, 0): (TextValue("genderqueer"),)}))) == 1
        )

    def test_synthetic_interviews_follow_the_same_toggle(self, session):
        """Or the cohort would be one thing on the map and another in the
        filter that drew it."""
        respondent(session, "Male", type=InterviewType.SYNTHETIC_TEST)
        session.flush()

        chosen = survey({(0, 0): (OptionValue(1),)})
        assert matching(session, chosen) == set()
        assert len(matching(session, chosen, include_synthetic=True)) == 1


class TestCheckboxes:
    def test_one_answer_holds_several_values(self, session):
        builder = Builder(session)
        builder.exchange("How do you travel?", "Bike, Bus", survey_item=TRANSPORT)
        session.flush()

        rows = answer_rows(session, PROJECT)
        assert values_of(rows[0]) == [OptionValue(0), OptionValue(1)]

    def test_it_matches_on_any_of_them(self, session):
        builder = Builder(session)
        builder.exchange("How do you travel?", "Bike, Bus", survey_item=TRANSPORT)
        builder.exchange("Why?", "It is quicker.", section=1, question=0)
        session.flush()

        assert len(matching(session, survey({(0, 0): (OptionValue(1),)}))) == 1
        assert matching(session, survey({(0, 0): (OptionValue(2),)})) == set()


class TestRanges:
    def test_a_number_is_filtered_by_range(self, session):
        for age in ("24", "31", "64"):
            builder = Builder(session)
            builder.exchange("Your age?", age, survey_item=AGE)
            session.flush()

        chosen = survey(ranges={(0, 0): Range(low="25", high="40")})
        assert len(matching(session, chosen)) == 1

    def test_an_end_may_be_open(self, session):
        for age in ("24", "31", "64"):
            builder = Builder(session)
            builder.exchange("Your age?", age, survey_item=AGE)
            session.flush()

        assert len(matching(session, survey(ranges={(0, 0): Range(low="25")}))) == 2
        assert len(matching(session, survey(ranges={(0, 0): Range(high="25")}))) == 1

    def test_a_date_is_filtered_the_same_way(self, session):
        for day in ("2024-01-05", "2024-08-05"):
            builder = Builder(session)
            builder.exchange("When did you start?", day, survey_item=STARTED)
            session.flush()

        chosen = survey(ranges={(0, 0): Range(low="2024-06-01", high="2024-12-31")})
        assert len(matching(session, chosen)) == 1

    def test_an_answer_that_is_not_a_date_is_left_out(self, session):
        """The item's type was changed after these interviews ran. Comparing a
        sentence to a date has no answer, so it is not in the cohort rather
        than at one end of it."""
        builder = Builder(session)
        builder.exchange("When did you start?", "last spring", survey_item=STARTED)
        session.flush()

        chosen = survey(ranges={(0, 0): Range(low="2000-01-01")})
        assert matching(session, chosen) == set()

    def test_a_bound_that_will_not_parse_is_no_bound(self, session):
        """Better than a filter nothing can satisfy: a typo would empty the
        view with nothing on screen to say why."""
        builder = Builder(session)
        builder.exchange("Your age?", "31", survey_item=AGE)
        session.flush()

        chosen = survey(ranges={(0, 0): Range(low="twenty-five")})
        assert len(matching(session, chosen)) == 1


class TestWhichAnswersCount:
    def test_a_declined_answer_is_not_a_value(self, session):
        builder = Builder(session)
        builder.exchange(
            "What is your gender?",
            CustomToken.skip_question.value,
            survey_item=GENDER,
            message_type=MessageType.CUSTOM_TOKEN,
        )
        session.flush()

        assert answer_rows(session, PROJECT) == []

    def test_a_question_a_condition_skipped_past_is_not_asked(self, session):
        """It was written to the transcript but never put to the respondent, so
        the message after it answers the *next* question."""
        builder = Builder(session)
        builder.say(
            MessageRole.ASSISTANT,
            "What is your gender?",
            survey_item=GENDER,
            skipped_by_condition=True,
        )
        builder.say(MessageRole.USER, "Male")
        session.flush()

        assert answer_rows(session, PROJECT) == []

    def test_a_probe_is_not_a_variable(self, session):
        """Only authored main questions are cohort variables: a follow-up is
        asked of some respondents and not others."""
        builder = Builder(session)
        builder.exchange(
            "And more precisely?", "Male", survey_item=GENDER, sub_question=1
        )
        session.flush()

        assert answer_rows(session, PROJECT) == []

    def test_a_free_text_question_offers_nothing_to_filter_by(self, session):
        builder = Builder(session)
        builder.exchange("How has the year been?", "Long.")
        session.flush()

        assert answer_rows(session, PROJECT) == []


def guide(session, *items):
    """A default localization holding one section of survey questions."""
    session.add(
        ProjectLocalizationTable(
            id=uuid.uuid4(),
            project_id=PROJECT,
            language="en",
            is_default=True,
            interview_guide=InterviewGuide(
                question_sections=[
                    QuestionSection(
                        description="About you",
                        questions=[
                            Question(main_question=prompt, survey_item=item)
                            for prompt, item in items
                        ],
                    )
                ]
            ),
        )
    )


class TestFacets:
    """What the picker is offered. Counts are interviews, because interviews
    are what selecting a value returns."""

    def facets(self, session, **kwargs):
        return survey_facets(session, PROJECT, kwargs.pop("include_synthetic", False))

    def test_an_option_nobody_chose_is_still_offered(self, session):
        """ "Nobody picked this" is a result, and seeing it beforehand is what
        keeps a filter from emptying the view for no visible reason."""
        guide(session, ("What is your gender?", GENDER))
        respondent(session, "Female")
        session.flush()

        [facet] = self.facets(session).items
        assert [(value.label, value.count) for value in facet.values] == [
            ("Female", 1),
            ("Male", 0),
            ("Non-binary", 0),
        ]

    def test_it_is_labelled_from_the_guide(self, session):
        guide(session, ("What is your gender?", GENDER))
        respondent(session, "Female")
        session.flush()

        [facet] = self.facets(session).items
        assert facet.question == "What is your gender?"
        assert facet.filter == "values"
        assert facet.type == "radio"

    def test_a_question_the_guide_no_longer_has_keeps_its_own_options(self, session):
        """The answers are still answers. Labelling them from a draft that no
        longer describes them would put a word in a respondent's mouth."""
        respondent(session, "Female")
        session.flush()

        [facet] = self.facets(session).items
        assert facet.question == ""
        assert [value.label for value in facet.values] == [
            "Female",
            "Male",
            "Non-binary",
        ]

    def test_a_write_in_is_offered_after_the_authored_options(self, session):
        guide(session, ("What is your gender?", GENDER))
        respondent(session, "Genderqueer")
        session.flush()

        [facet] = self.facets(session).items
        write_in = facet.values[-1]
        assert write_in.option is None
        assert (write_in.label, write_in.count) == ("genderqueer", 1)

    def test_a_checkbox_counts_interviews_and_not_answers(self, session):
        """One respondent holding two values is one interview each value can
        return, not two."""
        guide(session, ("How do you travel?", TRANSPORT))
        builder = Builder(session)
        builder.exchange("How do you travel?", "Bike, Bus", survey_item=TRANSPORT)
        session.flush()

        [facet] = self.facets(session).items
        assert facet.multiple is True
        assert [value.count for value in facet.values] == [1, 1, 0]
        assert facet.n_answered == 1

    def test_a_number_is_offered_as_the_range_it_spans(self, session):
        guide(session, ("Your age?", AGE))
        for age in ("24", "31", "64"):
            builder = Builder(session)
            builder.exchange("Your age?", age, survey_item=AGE)
            session.flush()

        [facet] = self.facets(session).items
        assert facet.filter == "range"
        assert (facet.low, facet.high) == ("24", "64")
        assert facet.n_answered == 3

    def test_a_free_text_question_is_not_a_facet(self, session):
        builder = Builder(session)
        builder.exchange("How has the year been?", "Long.")
        session.flush()

        assert self.facets(session).items == []


class TestParsingTheWireFormat:
    """The filter as it arrives on a URL. A malformed one is a 422 naming the
    problem rather than a filter for something other than what was asked."""

    def test_an_option_is_named_by_position(self):
        assert _parse_survey(["0,2=option:1"], None) == SurveyFilter(
            values={(0, 2): (OptionValue(1),)}, ranges={}
        )

    def test_a_write_in_is_named_by_its_text(self):
        assert _parse_survey(["0,2=text:Kayaking"], None) == SurveyFilter(
            values={(0, 2): (TextValue("kayaking"),)}, ranges={}
        )

    def test_values_of_one_item_gather_together(self):
        parsed = _parse_survey(["0,2=option:1", "0,2=option:0"], None)
        assert parsed is not None
        assert parsed.values == {(0, 2): (OptionValue(1), OptionValue(0))}

    def test_a_value_holding_an_equals_sign_survives(self):
        """Split on the first `=`, which no guide coordinate can contain."""
        parsed = _parse_survey(["0,2=text:a=b"], None)
        assert parsed is not None
        assert parsed.values == {(0, 2): (TextValue("a=b"),)}

    def test_a_range_may_have_an_open_end(self):
        assert _parse_survey(None, ["0,3=25.."]) == SurveyFilter(
            values={}, ranges={(0, 3): Range(low="25", high=None)}
        )

    def test_a_time_range_is_not_confused_by_its_colons(self):
        parsed = _parse_survey(None, ["0,3=09:00..17:00"])
        assert parsed is not None
        assert parsed.ranges == {(0, 3): Range(low="09:00", high="17:00")}

    def test_a_range_open_at_both_ends_asks_for_nothing(self):
        assert _parse_survey(None, ["0,3=.."]) is None

    def test_nothing_asked_for_is_no_filter(self):
        assert _parse_survey(None, None) is None

    @pytest.mark.parametrize(
        "values,ranges",
        [
            (["0,2"], None),
            (["0,2=1"], None),
            (["0,2=option:x"], None),
            (["0,2=option:-1"], None),
            (["nine=option:1"], None),
            (None, ["0,3=25"]),
        ],
    )
    def test_a_malformed_filter_is_rejected_by_name(self, values, ranges):
        with pytest.raises(HTTPException) as raised:
            _parse_survey(values, ranges)
        assert raised.value.status_code == 422


class TestFacetCohorts:
    """What the counts beside each option are counted over.

    The picker sits beside a list that says how many interviews it is drawn
    from. When the two disagree the reader has no way to tell which of them is
    wrong, so the counts follow the filters -- with one exception, which is the
    whole of what these are about.
    """

    def facets(self, session, scope=None, survey=None, include_synthetic=False):
        return survey_facets(session, PROJECT, include_synthetic, scope, survey).items

    def scope(self, session, **filters):
        return EmbeddingRepository(session).interviews_in_scope(
            PROJECT, EmbeddingFilters(**filters)
        )

    def counts(self, facet):
        return {value.label: value.count for value in facet.values}

    def test_the_counts_follow_the_interview_filter(self, session):
        guide(session, ("What is your gender?", GENDER))
        respondent(session, "Female", status="completed")
        respondent(session, "Male", status="completed")
        respondent(session, "Male", status="active")
        session.flush()

        scope = self.scope(session, status="completed")
        [facet] = self.facets(session, scope=scope)

        assert self.counts(facet) == {"Female": 1, "Male": 1, "Non-binary": 0}
        assert facet.n_answered == 2

    def test_an_item_does_not_narrow_its_own_counts(self, session):
        """The exception the design rests on. Counted with its own selection,
        picking "Male" would take the other options to zero and leave the
        reader unable to see what widening the choice would cost."""
        guide(session, ("What is your gender?", GENDER))
        respondent(session, "Female")
        respondent(session, "Male")
        session.flush()

        chosen = survey(values={(0, 0): (OptionValue(index=1),)})
        [facet] = self.facets(session, scope=self.scope(session), survey=chosen)

        assert self.counts(facet) == {"Female": 1, "Male": 1, "Non-binary": 0}

    def test_one_item_narrows_another(self, session):
        """Two items, so there is a cross-count to make: the transport item is
        counted over the men, and the gender item over everybody."""
        guide(
            session,
            ("What is your gender?", GENDER),
            ("How do you travel?", TRANSPORT),
        )
        for gender, transport in (("Female", "Bike"), ("Male", "Bus"), ("Male", "Car")):
            builder = Builder(session)
            builder.exchange("What is your gender?", gender, survey_item=GENDER)
            builder.exchange(
                "How do you travel?", transport, question=1, survey_item=TRANSPORT
            )
        session.flush()

        chosen = survey(values={(0, 0): (OptionValue(index=1),)})
        gender, travel = self.facets(session, scope=self.scope(session), survey=chosen)

        assert self.counts(gender) == {"Female": 1, "Male": 2, "Non-binary": 0}
        assert self.counts(travel) == {"Bike": 0, "Bus": 1, "Car": 1}

    def test_an_option_the_cohort_leaves_empty_is_still_offered(self, session):
        """Zero rather than absent: a filter must never remove the control that
        would undo it."""
        guide(session, ("What is your gender?", GENDER))
        respondent(session, "Female", status="completed")
        respondent(session, "Male", status="active")
        session.flush()

        [facet] = self.facets(session, scope=self.scope(session, status="completed"))

        assert self.counts(facet) == {"Female": 1, "Male": 0, "Non-binary": 0}

    def test_a_range_is_read_off_the_cohort_too(self, session):
        guide(session, ("How old are you?", AGE))
        for age, status in (("25", "completed"), ("60", "active")):
            builder = Builder(session, status=status)
            builder.exchange("How old are you?", age, survey_item=AGE)
        session.flush()

        [facet] = self.facets(session, scope=self.scope(session, status="completed"))

        assert (facet.low, facet.high) == ("25", "25")

    def test_the_scope_the_endpoint_builds_leaves_room_for_the_exemption(self, session):
        """The composition, not just the pieces. Building the scope with the
        survey filter already applied and then handing the same filter over for
        cross-counting takes every other option to zero -- the exemption has
        nothing left to give back. This is that mistake, spelled as a test."""
        guide(session, ("What is your gender?", GENDER))
        respondent(session, "Female", status="completed")
        respondent(session, "Male", status="completed")
        session.flush()

        chosen = survey(values={(0, 0): (OptionValue(index=1),)})
        # As `read_survey_facets` composes it: every filter but the survey one.
        scope = self.scope(session, status="completed")
        [facet] = self.facets(session, scope=scope, survey=chosen)

        assert self.counts(facet) == {"Female": 1, "Male": 1, "Non-binary": 0}

    def test_no_scope_is_the_whole_project(self, session):
        """The picker is also read where nothing is filtering it."""
        guide(session, ("What is your gender?", GENDER))
        respondent(session, "Female")
        respondent(session, "Male")
        session.flush()

        [facet] = self.facets(session)

        assert self.counts(facet) == {"Female": 1, "Male": 1, "Non-binary": 0}
