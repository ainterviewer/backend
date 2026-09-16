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
    CodeTerm,
    KeywordQueryError,
    Node,
    Not,
    Or,
    Term,
    compile_condition,
    excluded_spans,
    leaves_of,
    match_spans,
    parse,
    resolve_scope,
    term_pattern,
    terms_of,
    without_code_terms,
)


def tree(query: str) -> Node:
    """`parse`, for a query that is known to be one.

    `parse` widens to None for a query that asks nothing -- an empty box --
    and none of these are that. Asserting it here rather than at twenty call
    sites keeps the tests about the grammar, and the checker satisfied that a
    tree is what reached the function under test.
    """
    node = parse(query)
    assert node is not None
    return node


def leaf(query: str) -> Term | CodeTerm:
    """`tree`, for a query that is a single leaf.

    What the scope rules are asked about: a scope belongs to a term, not to an
    expression, so anything with an operator in it is not the question.
    """
    node = tree(query)
    assert isinstance(node, (Term, CodeTerm))
    return node


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


class TestMarkup:
    """Guide text may carry markup; nobody said the markup.

    A keyword scan runs against the raw message, so `stress*` matches
    "Stressand" inside a support page's address. Marking that claims a
    respondent said a word they did not, and the SQL side drops the same runs
    so a row is never selected on evidence a reader cannot see.
    """

    OUTRO = 'guidance about stress <a href="https://ku.dk/Stressand-x.aspx">KUnet</a>.'

    def spans(self, text, query, side):
        return match_spans(text, parse(query), side, "both")

    def test_a_match_inside_a_tag_is_not_a_match(self):
        found = self.spans(self.OUTRO, "stress*", "question")

        assert [self.OUTRO[a:b] for a, b in found] == ["stress"]

    def test_the_prose_around_a_tag_still_matches(self):
        assert self.spans("say <b>this</b> now", "now", "question")

    def test_a_term_matching_only_a_tag_name_finds_nothing(self):
        # `<u>` is a real tag in this project's guide, and `u` is a term a
        # boundary-matched query will happily find inside it.
        assert self.spans("work <u>on average</u> per week", "u", "question") == []

    def test_respondent_markup_is_text_and_still_matches(self):
        """A respondent who types `<b>` is shown those characters, so a term
        that matched them matched something they can see."""
        assert self.spans("I wrote <b>bold</b> here", "b", "answer")

    def test_an_excluded_word_is_reported_in_prose_but_not_in_a_tag(self):
        """Both halves at once: the word the query excluded really is in the
        sentence and is marked there, and the one in the URL is not."""
        node = parse("KUnet -stress")
        marks = match_spans(self.OUTRO, node, "question", "question")
        excluded = excluded_spans(self.OUTRO, node, marks, "question")

        assert [self.OUTRO[a:b] for a, b in excluded] == ["stress"]
        assert all(b <= self.OUTRO.index("<a href") for _, b in excluded)

    def test_a_quoted_attribute_may_contain_a_bracket(self):
        """The reason the pattern is not `<[^>]*>`: a `>` inside quotes does
        not end the tag, and treating it as if it did would leave half a tag
        looking like prose."""
        text = '<a title="a > b">stress</a>'

        assert [text[a:b] for a, b in self.spans(text, "stress", "question")] == [
            "stress"
        ]


class TestCodeTerms:
    """`code:`, which names something in the codebook rather than a word.

    What a code term means once it reaches SQL is `test_browse.py`'s business.
    Here it is only the shape it parses to, and the two places it deliberately
    behaves unlike a `Term`: it is invisible to the highlighter, and it does not
    take the caller's default scope.
    """

    def test_a_bare_name(self):
        assert parse("code:stress") == CodeTerm(("stress",))

    def test_a_star_suffix_asks_for_the_subtree(self):
        """Explicit, so that a parent which is *applied* and a parent which only
        organises are not the same query. `CodeKind.GROUP` is how a branch says
        it is a container."""
        assert parse("code:stress/*") == CodeTerm(("stress",), subtree=True)

    def test_a_path_separates_on_slashes(self):
        assert parse("code:Stress/Often") == CodeTerm(("Stress", "Often"))

    def test_quotes_carry_a_name_with_spaces(self):
        assert parse('code:"Stress/New code"') == CodeTerm(("Stress", "New code"))

    def test_the_subtree_marker_sits_outside_the_quotes(self):
        """Quotes delimit the name; `/*` is grammar."""
        assert parse('code:"Stress/New code"/*') == CodeTerm(
            ("Stress", "New code"), subtree=True
        )

    def test_a_star_inside_quotes_is_the_character(self):
        """Quoting is how a reader asks for the literal thing, exactly as it is
        for a phrase."""
        assert parse('code:"a*b"') == CodeTerm(("a*b",))

    def test_steps_are_trimmed(self):
        assert parse('code:"Stress / Often"') == CodeTerm(("Stress", "Often"))

    def test_a_prefix_scopes_a_code_term(self):
        assert parse("a:code:stress") == CodeTerm(
            ("stress",), scope="answer", position=2
        )
        assert parse("q:code:stress") == CodeTerm(
            ("stress",), scope="question", position=2
        )

    def test_a_prefix_distributes_over_a_group_of_mixed_leaves(self):
        assert parse("a:(code:stress OR kids)") == Or(
            (
                CodeTerm(("stress",), scope="answer", position=3),
                Term("kids", scope="answer"),
            )
        )

    def test_a_code_term_survives_negation(self):
        assert parse("-code:stress") == Not(CodeTerm(("stress",), position=1))

    def test_it_composes_with_words(self):
        assert parse("code:stress kids") == And((CodeTerm(("stress",)), Term("kids")))

    def test_a_name_after_the_prefix_is_never_an_operator(self):
        """The reference is read before the bare-term scan, so the scan's rule
        about operators never sees it."""
        assert parse("code:or") == CodeTerm(("or",))

    def test_the_prefix_is_case_insensitive(self):
        assert parse("CODE:stress") == CodeTerm(("stress",))


class TestCodeScope:
    """The one rule that keeps interviewer-turn codings findable."""

    def test_a_bare_code_term_means_both_sides(self):
        """Never the caller's default. The segmented control governs *words*;
        taking it here would mean its ordinary setting silently dropped every
        coding made on an interviewer turn."""
        assert resolve_scope(leaf("code:stress"), "answer") == "both"
        assert resolve_scope(leaf("code:stress"), "question") == "both"

    def test_a_written_prefix_still_wins(self):
        assert resolve_scope(leaf("a:code:stress"), "question") == "answer"
        assert resolve_scope(leaf("q:code:stress"), "answer") == "question"

    def test_a_word_still_takes_the_default(self):
        """The divergence is the code term's alone."""
        assert resolve_scope(leaf("stress"), "question") == "question"


class TestCodeTermsAndHighlighting:
    def test_terms_of_ignores_code_terms(self):
        """`terms_of` feeds the highlighter, and a code term has no pattern to
        mark. What it selects on is a coding row, whose own offsets are a
        separate matter."""
        node = tree("code:stress AND kids")
        assert [term.text for term in terms_of(node)] == ["kids"]

    def test_nothing_is_marked_for_a_code_term_alone(self):
        assert (
            match_spans("jeg er stresset", parse("code:stress"), "answer", "answer")
            == []
        )

    def test_a_sibling_word_is_still_marked(self):
        text = "jeg er stresset"
        spans = match_spans(text, parse("code:x AND stresset"), "answer", "answer")
        assert [text[start:end] for start, end in spans] == ["stresset"]

    def test_a_negated_code_term_is_not_an_excluded_span(self):
        assert excluded_spans("jeg er stresset", parse("-code:stresset"), []) == []

    def test_leaves_of_counts_both_kinds(self):
        node = tree("code:stress AND kids OR -code:x")
        assert len(leaves_of(node)) == 3

    def test_code_terms_count_towards_the_cap(self):
        query = " OR ".join(f"code:c{index}" for index in range(MAX_TERMS + 1))
        with pytest.raises(KeywordQueryError) as raised:
            parse(query)
        assert "Too many search terms" in raised.value.message


class TestCodeErrors:
    @pytest.mark.parametrize(
        ("query", "fragment"),
        [
            ("code:", "needs the name of a code"),
            ("code: stress", "needs the name of a code"),
            ("code:*", "written as “/*” at the end"),
            ("code:stre*", "written as “/*” at the end"),
            ("code:a//b", "empty step"),
            ("code:/b", "empty step"),
            ('code:"unclosed', "Unclosed quote"),
        ],
    )
    def test_says_what_is_wrong(self, query, fragment):
        with pytest.raises(KeywordQueryError) as raised:
            parse(query)
        assert fragment in raised.value.message
        assert raised.value.position is not None

    def test_a_bare_prefix_is_an_error_unlike_a_bare_scope(self):
        """`q:` alone is the ordinary word "q:", because somebody might mean it.
        `code:` alone is a half-typed reference and nothing else, so naming that
        beats searching for a word nobody wrote."""
        assert parse("q:") == Term("q:")
        with pytest.raises(KeywordQueryError):
            parse("code:")

    def test_a_long_step_is_refused(self):
        with pytest.raises(KeywordQueryError) as raised:
            parse("code:" + "x" * 500)
        assert "too long" in raised.value.message

    def test_compiling_without_a_builder_refuses_rather_than_matches(self):
        """The honest failure. Compiling a code term away would return every
        chunk and call it a match."""
        with pytest.raises(KeywordQueryError) as raised:
            compile_condition(tree("code:stress"), None, None)
        assert "not available" in raised.value.message

    def test_a_builder_is_used_where_one_is_given(self):
        seen = []

        def build(term):
            seen.append(term)
            return True

        compile_condition(tree("code:stress/*"), None, None, code=build)
        assert seen == [CodeTerm(("stress",), subtree=True)]


class TestPruningCodeTerms:
    """`without_code_terms`, which the code facets are counted under.

    Its one hard requirement is that a pruned tree is never *more* restrictive
    than the tree it came from -- a badge counting fewer rows than the filter
    it offers would send the reader somewhere empty.
    """

    def pruned(self, query: str):
        return without_code_terms(tree(query))

    def test_a_bare_code_term_leaves_nothing(self) -> None:
        assert self.pruned("code:stress") is None

    def test_a_negated_code_term_leaves_nothing(self) -> None:
        # The case substitution gets wrong: `NOT True` is false everywhere, so
        # replacing the leaf instead of removing it would empty every badge.
        assert self.pruned("-code:stress") is None

    def test_text_survives_beside_a_code_term(self) -> None:
        assert self.pruned("code:stress AND kids") == Term("kids")

    def test_several_code_terms_all_go(self) -> None:
        assert self.pruned("code:stress AND -code:workload") is None

    def test_the_operator_is_kept_when_more_than_one_operand_remains(self) -> None:
        assert self.pruned("code:stress OR kids OR skole") == Or(
            (Term("kids"), Term("skole"))
        )
        assert self.pruned("code:stress AND kids AND skole") == And(
            (Term("kids"), Term("skole"))
        )

    def test_a_group_that_empties_takes_its_operator_with_it(self) -> None:
        assert self.pruned("(code:stress OR code:workload) AND kids") == Term("kids")

    def test_a_negated_group_keeps_what_is_left_negated(self) -> None:
        assert self.pruned("-(code:stress OR kids)") == Not(Term("kids"))

    def test_a_query_with_no_code_terms_is_returned_unchanged(self) -> None:
        node = tree("kids AND -skole")
        assert without_code_terms(node) == node

    def test_scope_is_carried_through(self) -> None:
        # The prefix distributes at parse time, so pruning must not undo it:
        # counting `q:kids` as plain `kids` would widen the corpus.
        assert self.pruned("q:(code:stress OR kids)") == Term("kids", scope="question")
