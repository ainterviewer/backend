"""Tests for the keyword query language.

Pure parsing and pattern building -- no database. What the language means once
it reaches SQL is `test_browse.py`'s `TestBooleanKeywords`.

The error cases matter as much as the parses: a query that cannot be read is
shown to the reader as a message pointing at a character, so both the message
and the position are part of the contract.
"""

import re

import pytest

from app.db.keyword_query import (
    MAX_TERMS,
    And,
    KeywordQueryError,
    Not,
    Or,
    Term,
    parse,
    term_pattern,
    terms_of,
)


def matches(query: str, text: str) -> bool:
    """Whether `text` satisfies a single-term query, as the database would.

    Only good for one term: composing the tree is SQL's job, not the regular
    expression's. Case-insensitive because that is how the condition is
    compiled -- `~*` on Postgres, `re.IGNORECASE` in the SQLite function.
    """
    node = parse(query)
    assert isinstance(node, Term)
    return re.search(term_pattern(node), text, re.IGNORECASE) is not None


class TestShape:
    def test_a_bare_word_is_a_term(self):
        assert parse("dog") == Term("dog")

    def test_adjacency_is_and(self):
        assert parse("climate change") == And((Term("climate"), Term("change")))

    def test_or_is_asked_for(self):
        assert parse("dog OR cat") == Or((Term("dog"), Term("cat")))

    def test_minus_is_not(self):
        assert parse("kids -school") == And((Term("kids"), Not(Term("school"))))

    def test_and_binds_tighter_than_or(self):
        """`a OR b AND c` is `a OR (b AND c)`, the usual precedence."""
        assert parse("a OR b AND c") == Or((Term("a"), And((Term("b"), Term("c")))))

    def test_brackets_override_precedence(self):
        assert parse("(a OR b) AND c") == And((Or((Term("a"), Term("b"))), Term("c")))

    def test_symbols_spell_the_operators_too(self):
        assert parse("a && b") == parse("a AND b")
        assert parse("a || b") == parse("a OR b")
        assert parse("!a") == parse("NOT a")

    def test_operator_words_are_case_insensitive(self):
        assert parse("dog or cat") == parse("dog OR cat")

    def test_quoting_makes_an_operator_a_word(self):
        assert parse('"or"') == Term("or", phrase=True)

    def test_an_empty_query_asks_for_nothing(self):
        assert parse("") is None
        assert parse("   ") is None


class TestTerms:
    def test_a_hyphen_inside_a_word_stays(self):
        """Only a *leading* hyphen negates. `e-mail` is a word people write."""
        assert parse("e-mail") == Term("e-mail")

    def test_a_trailing_star_opens_the_end(self):
        assert parse("kat*") == Term("kat", open_end=True)

    def test_a_leading_star_opens_the_start(self):
        assert parse("*kat") == Term("kat", open_start=True)

    def test_stars_on_both_sides_are_a_substring(self):
        assert parse("*kat*") == Term("kat", open_start=True, open_end=True)

    def test_a_star_in_the_middle_is_a_character(self):
        """A respondent writing "3*4" wrote a multiplication sign."""
        assert parse("3*4") == Term("3*4")

    def test_a_phrase_keeps_its_spaces(self):
        assert parse('"my neighbour"') == Term("my neighbour", phrase=True)

    def test_terms_of_walks_the_whole_tree(self):
        node = parse("(a OR b) AND NOT c")
        assert node is not None
        assert [term.text for term in terms_of(node)] == ["a", "b", "c"]


class TestMatching:
    def test_a_term_matches_a_word_not_a_substring(self):
        assert matches("kat", "en kat her")
        assert not matches("kat", "katalog")
        assert not matches("kat", "delikat")

    def test_punctuation_is_a_boundary(self):
        assert matches("kat", "Kat.")
        assert matches("yes", "Yes, entirely.")

    def test_stars_relax_the_boundary_they_are_on(self):
        assert matches("kat*", "katalog")
        assert not matches("kat*", "delikat")
        assert matches("*kat", "delikat")
        assert not matches("*kat", "katalog")
        assert matches("*kat*", "delikat")
        assert matches("*kat*", "katalog")

    def test_matching_ignores_case(self):
        assert matches("funding", "The Funding ran out.")

    def test_regex_metacharacters_are_literal(self):
        """Escaped, or `.` would match anything and `100%` would be a wildcard
        in the LIKE this replaced."""
        assert matches("100%", "we reached 100% of it")
        assert not matches("100%", "we reached 100 of it")
        assert matches("a.b", "the file a.b here")
        assert not matches("a.b", "the file axb here")

    def test_a_word_ending_in_punctuation_is_not_glued_to_the_next(self):
        """The reason boundaries are lookarounds and not `\\b`: after the `%`
        of "100%", `\\b` would demand a word character next."""
        assert matches("100%", "exactly 100%")
        assert not matches("100%", "exactly 100%x")

    def test_a_phrase_matches_across_whitespace(self):
        assert matches('"my neighbour"', "My\n  neighbour said so")

    def test_danish_letters_are_word_characters(self):
        assert matches("børn", "vores børn")
        assert not matches("børn", "børnehave")
        assert matches("børn*", "børnehave")


class TestScopes:
    """`q:` and `a:`, which say which side of the exchange to look in."""

    def test_a_prefix_scopes_one_term(self):
        assert parse("q:stress") == Term("stress", scope="question")
        assert parse("a:stress") == Term("stress", scope="answer")

    def test_a_prefix_scopes_a_phrase(self):
        assert parse('q:"min nabo"') == Term("min nabo", phrase=True, scope="question")

    def test_a_prefix_distributes_over_a_group(self):
        """The way a prefix behaves in every other search box: `q:(a OR b)` is
        `q:a OR q:b`, not an error about brackets."""
        assert parse("q:(a OR b)") == Or(
            (Term("a", scope="question"), Term("b", scope="question"))
        )

    def test_the_nearer_prefix_wins(self):
        """A scope written further in is the more deliberate of the two."""
        assert parse("q:(a OR a:b)") == Or(
            (Term("a", scope="question"), Term("b", scope="answer"))
        )

    def test_a_prefix_survives_negation(self):
        assert parse("-q:stress") == Not(Term("stress", scope="question"))

    def test_a_word_after_a_prefix_is_never_an_operator(self):
        assert parse("q:or") == Term("or", scope="question")

    def test_a_bare_prefix_is_an_ordinary_word(self):
        # Nothing follows it, so there is nothing for it to scope.
        assert parse("q:") == Term("q:")

    def test_an_unscoped_term_keeps_no_scope(self):
        """Resolved against the caller's default at compile time, not here, so
        one parse can serve either setting of the control."""
        assert parse("stress") == Term("stress")


class TestErrors:
    @pytest.mark.parametrize(
        ("query", "position"),
        [
            ("(dog OR cat", 0),
            ("dog)", 3),
            ("dog AND", 4),
            ("AND dog", 0),
            ("()", 0),
            ('"unclosed', 0),
            ('""', 0),
            ("-", 0),
            ("*", 0),
            ("dog OR OR cat", 4),
        ],
    )
    def test_points_at_the_problem(self, query, position):
        with pytest.raises(KeywordQueryError) as raised:
            parse(query)
        assert raised.value.position == position
        assert raised.value.message

    def test_too_many_terms_is_refused(self):
        query = " OR ".join(f"w{index}" for index in range(MAX_TERMS + 1))
        with pytest.raises(KeywordQueryError) as raised:
            parse(query)
        assert "Too many search terms" in raised.value.message

    def test_deep_nesting_is_refused(self):
        query = "(" * 40 + "dog" + ")" * 40
        with pytest.raises(KeywordQueryError) as raised:
            parse(query)
        assert "nested brackets" in raised.value.message
