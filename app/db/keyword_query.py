"""A small boolean query language for keyword search.

The keyword box in the explore view accepts more than one word:

    dog OR cat
    dog AND NOT puppy
    (dog OR cat) AND "my neighbour"
    kids -school
    climate change

Adjacent terms mean AND, because that is what a reader typing two words into a
search box means. `OR` and `NOT` have to be asked for, `-` is shorthand for
`NOT`, and parentheses group.

By default a term is matched against what the respondent answered. `q:` and `a:`
override that for one term -- `q:stress a:træt` finds answers saying "træt" to
questions about stress, which no single toggle can express -- and the caller's
`default_scope` says what a bare term means.

`code:` names a code from the project's codebook rather than a word, so the same
expression can ask about what was said and what was marked:

    code:stress                 coded with Stress, that code exactly
    code:stress/*               Stress and everything under it
    code:"Stress/New code"      by path, where a bare name is ambiguous
    a:code:stress               coded on the answer rather than the question
    code:stress -code:workload  composes with NOT like any other term

A code term takes a written `q:`/`a:` but never the caller's default: the
default governs words, and applying it here would silently drop codings made on
an interviewer turn. See `resolve_scope`. Names are resolved by the caller, not
here -- this module has no codebook -- so `compile_condition` takes a builder.

A bare term matches a *word*, not a substring: `kat` finds "kat" and "Kat." but
not "katalog". A trailing or leading `*` opens that edge -- `kat*` matches
"katalog", `*kat` matches "delikat", `*kat*` matches anywhere -- and a quoted
phrase matches those words in that order, with any run of whitespace between
them, so a phrase still matches across a line break.

Matching is always case-insensitive. There is no case-sensitive mode to get
wrong: respondents type freely, and the dev database is SQLite whose `LIKE` is
case-insensitive whatever it is asked for, so a case-sensitive promise could
only be kept in production -- which is the worst place to discover it.

Parsing is separate from compiling to SQL so the API layer can reject a
malformed query with a message that names the problem and where it is, rather
than searching for something other than what was written.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Literal

from sqlalchemy import and_, not_, or_

__all__ = [
    "And",
    "CodeTerm",
    "KeywordQueryError",
    "Node",
    "Not",
    "Or",
    "Term",
    "leaves_of",
    "parse",
    "terms_of",
]

#: How many terms one query may contain, and how deeply it may nest. Both are
#: far past what anyone types by hand and well short of what would make the
#: generated SQL a problem; they exist so a pasted or generated string cannot
#: turn into an unbounded query.
MAX_TERMS = 64
MAX_DEPTH = 16

#: The longest a single term may be. Only a guard against nonsense -- the query
#: as a whole is already capped by the endpoint's `max_length`.
MAX_TERM_LENGTH = 200


class KeywordQueryError(ValueError):
    """A query that cannot be parsed, and where it went wrong.

    `position` is a zero-based offset into the original string, for a client
    that wants to point at the character. It is None where the problem is the
    query as a whole rather than one place in it.
    """

    def __init__(self, message: str, position: int | None = None):
        super().__init__(message)
        self.message = message
        self.position = position


# --------------------------------------------------------------------------
# Syntax tree
# --------------------------------------------------------------------------


#: Which side of the exchange a term is matched against. `None` on a parsed term
#: means "whatever the caller's default is" -- the segmented control in the UI --
#: and is resolved when the tree is compiled, so one parse can serve either.
Scope = Literal["answer", "question", "both"]

#: What the `q:` and `a:` prefixes mean. Spelled short because they are typed
#: mid-query; spelled in English rather than Danish because the operators are.
_SCOPE_PREFIXES: dict[str, Scope] = {"q": "question", "a": "answer"}


@dataclass(frozen=True)
class Term:
    """One thing to look for.

    `text` is literal -- any regex or LIKE metacharacter in it is escaped when
    it is compiled, so a respondent writing "100%" is searched for as a percent
    sign rather than as a wildcard.

    `open_start` and `open_end` record a `*` on that edge, meaning the match
    need not begin (or end) at a word boundary there.

    `phrase` records that this came from quotes, which changes only how internal
    whitespace is treated: a phrase matches across any run of it.
    """

    text: str
    open_start: bool = False
    open_end: bool = False
    phrase: bool = False
    #: `q:` or `a:` written on this term, or None to take the default.
    scope: Scope | None = None


@dataclass(frozen=True)
class CodeTerm:
    """One code from the project's codebook, as written.

    A leaf beside `Term` rather than another `Scope` value, because the two are
    not the same kind of thing: a scope picks *which column* a word is matched
    against, and this matches no text at all. It selects on whether a passage
    was *coded*, which is a different table.

    `path` is the code as written, already split on `/`: `("Stress",)` for
    `code:stress`, `("Stress", "Often")` for `code:"Stress/Often"`. Names rather
    than ids because a parser has no codebook -- resolution happens in the
    endpoint, which does, and which can then say *which* name it could not
    place. A codebook may hold three codes called "New code", so an ambiguous
    name has to be reportable rather than guessable.

    `subtree` records the `/*` suffix. Without it the term means this code
    exactly, which lines the filter up with `CodeKind.GROUP`: a branch that is
    only a container says so by being a GROUP, and is not inferred from having
    children.

    `position` is carried, unlike on `Term`, because a code term can still fail
    *after* parsing -- on a name that matches nothing or matches twice -- and
    the 422 for that should point at the same character a syntax error would.
    """

    path: tuple[str, ...]
    #: `/*` was written: this code and every code under it.
    subtree: bool = False
    #: `q:` or `a:` written on this term. None means *both sides* -- never the
    #: caller's default. See `resolve_scope` for why that differs from `Term`.
    scope: Scope | None = None
    #: Where the `code:` prefix began, for naming an unresolvable code.
    position: int = 0


@dataclass(frozen=True)
class Not:
    operand: Node


@dataclass(frozen=True)
class And:
    operands: tuple[Node, ...]


@dataclass(frozen=True)
class Or:
    operands: tuple[Node, ...]


Node = Term | CodeTerm | Not | And | Or


def terms_of(node: Node) -> list[Term]:
    """Every *text* term in the tree, in the order written.

    Deliberately not the code terms: this feeds the highlighter, which marks
    where a pattern matched, and a code term has no pattern to mark. What it
    selects on is a coding row, whose own offsets are a separate matter.

    `leaves_of` is the one to count with.
    """
    if isinstance(node, Term):
        return [node]
    if isinstance(node, CodeTerm):
        return []
    if isinstance(node, Not):
        return terms_of(node.operand)
    return [term for operand in node.operands for term in terms_of(operand)]


def leaves_of(node: Node) -> list[Term | CodeTerm]:
    """Every leaf in the tree, of either kind.

    What `MAX_TERMS` is counted over: a query of two hundred code terms costs
    what a query of two hundred words costs, and the cap exists to bound the
    generated SQL rather than to bound how many *words* were asked for.
    """
    if isinstance(node, (Term, CodeTerm)):
        return [node]
    if isinstance(node, Not):
        return leaves_of(node.operand)
    return [leaf for operand in node.operands for leaf in leaves_of(operand)]


def without_code_terms(node: Node) -> Node | None:
    """`node` with every code leaf pruned, or None if nothing survives.

    What the code facets are counted under: every other filter still applies,
    but a code term must not, or activating `code:stress` would take every
    other code's badge to zero and leave the reader inside a selection they
    can no longer see out of -- the same rule `_facet_cohorts` keeps for
    survey items.

    The leaves are *pruned*, never replaced by a true condition. Substitution
    breaks under negation: `-code:stress` would become `NOT True`, which is
    false everywhere, and the badges would read zero across the codebook.

    Under `OR` this narrows rather than widens -- `code:x OR kids` counts over
    `kids` alone. There is no reading of "the same query without the code
    part" that a disjunction agrees with, and pruning is at least the one that
    never counts rows the reader cannot reach.
    """
    if isinstance(node, CodeTerm):
        return None
    if isinstance(node, Term):
        return node
    if isinstance(node, Not):
        operand = without_code_terms(node.operand)
        return Not(operand) if operand is not None else None

    kept = tuple(
        pruned
        for pruned in (without_code_terms(operand) for operand in node.operands)
        if pruned is not None
    )
    if not kept:
        return None
    if len(kept) == 1:
        return kept[0]
    return type(node)(kept)


# --------------------------------------------------------------------------
# Tokenizer
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Token:
    kind: str  # 'term' | 'phrase' | 'code' | 'and' | 'or' | 'not' | 'scope' | '(' | ')'
    text: str
    position: int
    scope: Scope | None = None
    #: On a 'code' token: the reference, split on `/` and already checked.
    path: tuple[str, ...] | None = None
    #: On a 'code' token: whether `/*` was written.
    subtree: bool = False


#: Operator spellings. The words are recognised case-insensitively: a reader who
#: types `dog or cat` means the same thing as `dog OR cat`, and requiring
#: capitals would mostly produce a search for the literal word "or". Quoting
#: escapes them -- `"or"` is the word.
_WORD_OPERATORS = {
    "and": "and",
    "or": "or",
    "not": "not",
    "&&": "and",
    "||": "or",
}

#: Characters that end a bare term. Everything else, punctuation included, is
#: part of it: `e-mail` and `kl. 12` are words people write.
_BREAKS = set(' \t\r\n\f\v()"')

#: What introduces a code reference. Long enough to be unmistakable in a box
#: that is mostly words, and read before the bare-term scan -- see `_tokenize`.
_CODE_PREFIX = "code:"

#: The suffix that widens a code reference to its descendants.
_SUBTREE_SUFFIX = "/*"


def _tokenize(query: str) -> list[_Token]:
    tokens: list[_Token] = []
    index = 0
    length = len(query)

    while index < length:
        char = query[index]

        if char.isspace():
            index += 1
            continue

        if char in "()":
            tokens.append(_Token(char, char, index))
            index += 1
            continue

        # A `q:` or `a:` prefix binds to the one term that follows, phrase
        # included, so `q:"min nabo"` is a phrase looked for in the question.
        # `q:` / `a:` scope whatever follows them -- one term, a phrase, or a
        # whole bracketed group, which is how every other search box people use
        # behaves. Emitted as its own token so the parser can push it down.
        if (
            query[index : index + 2].lower() in ("q:", "a:")
            and index + 2 < length
            and not query[index + 2].isspace()
        ):
            tokens.append(
                _Token(
                    "scope",
                    query[index : index + 2],
                    index,
                    _SCOPE_PREFIXES[query[index].lower()],
                )
            )
            index += 2
            continue

        # `code:` names something in the codebook rather than a word to look
        # for, so it is read here rather than falling through to the bare-term
        # scan -- which would swallow the whole reference, colon and all.
        #
        # Before that scan for a second reason: the scan reads a word after a
        # scope as a term and never as an operator, so leaving this later would
        # make `a:code:stress` tokenize as the bare word "code:stress".
        if query[index : index + len(_CODE_PREFIX)].lower() == _CODE_PREFIX:
            token, index = _code_token(query, index)
            tokens.append(token)
            continue

        if char == '"':
            end = query.find('"', index + 1)
            if end == -1:
                raise KeywordQueryError('Unclosed quote — add a closing ".', index)
            text = query[index + 1 : end].strip()
            if not text:
                raise KeywordQueryError(
                    "Empty quotes — put the phrase you are looking for inside them.",
                    index,
                )
            tokens.append(_Token("phrase", text, index))
            index = end + 1
            continue

        # A leading `-` or `!` is negation rather than part of the word, so
        # `kids -school` reads the way it does in every other search box. Only
        # leading: `e-mail` keeps its hyphen, and so does `!` inside a word.
        # `-q:stress` therefore reaches the prefix on the next pass.
        if char in "-!":
            tokens.append(_Token("not", char, index))
            index += 1
            continue

        start = index
        while index < length and query[index] not in _BREAKS:
            index += 1
        raw = query[start:index]

        # A word after a scope is a term, never an operator: `q:or` is the word
        # "or" looked for in the question.
        after_scope = bool(tokens) and tokens[-1].kind == "scope"
        operator = None if after_scope else _WORD_OPERATORS.get(raw.lower())
        if operator is not None:
            tokens.append(_Token(operator, raw, start))
        else:
            tokens.append(_Token("term", raw, start))

    return tokens


def _code_token(query: str, start: int) -> tuple[_Token, int]:
    """The code reference beginning at `start`, and the index after it.

    Two spellings, because a code's name is free text that may contain spaces
    or collide with another code's:

        code:stress              a name
        code:Stress/Often        a path, where the name alone is ambiguous
        code:"Stress/New code"   quoted, where a step contains spaces

    A trailing `/*` widens the reference to the code's descendants and sits
    *outside* any quotes: the quotes delimit the name, the marker is grammar.

    Inside quotes `*` is the character, exactly as it is in a quoted phrase --
    quoting is how a reader asks for the literal thing. Unquoted it is rejected
    unless it is the `/*` suffix, because the alternative is `code:stre*`
    looking like a wildcard, parsing cleanly, and then failing much later as a
    name that matches nothing.
    """
    index = start + len(_CODE_PREFIX)
    length = len(query)
    quoted = index < length and query[index] == '"'

    if quoted:
        end = query.find('"', index + 1)
        if end == -1:
            raise KeywordQueryError('Unclosed quote — add a closing ".', index)
        text = query[index + 1 : end]
        index = end + 1
        subtree = query[index : index + 2] == _SUBTREE_SUFFIX
        if subtree:
            index += 2
    else:
        text_start = index
        while index < length and query[index] not in _BREAKS:
            index += 1
        text = query[text_start:index]
        # The scan runs straight through `/*`, neither character being a break.
        subtree = text.endswith(_SUBTREE_SUFFIX)
        if subtree:
            text = text[: -len(_SUBTREE_SUFFIX)]

    if not text.strip():
        raise KeywordQueryError(
            "“code:” needs the name of a code after it — for example code:stress.",
            start,
        )
    if not quoted and "*" in text:
        raise KeywordQueryError(
            "“*” after a code means “and everything under it”, and is written "
            "as “/*” at the end — for example code:stress/*.",
            start,
        )

    steps = tuple(step.strip() for step in text.split("/"))
    if any(not step for step in steps):
        raise KeywordQueryError(
            "A code path cannot have an empty step — write it like "
            'code:"Stress/Often".',
            start,
        )
    for step in steps:
        if len(step) > MAX_TERM_LENGTH:
            raise KeywordQueryError(
                f"“{step[:20]}…” is too long (at most {MAX_TERM_LENGTH} characters).",
                start,
            )

    return _Token("code", text, start, path=steps, subtree=subtree), index


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


class _Parser:
    """Recursive descent over the token list.

        query   := or
        or      := and (OR and)*
        and     := unary (AND? unary)*
        unary   := (NOT | '-') unary | ('q:' | 'a:') unary | primary
        primary := '(' or ')' | phrase | term | code

    Precedence is the usual one: NOT binds tighter than AND, which binds tighter
    than OR, so `a OR b AND c` is `a OR (b AND c)`.
    """

    def __init__(self, tokens: list[_Token], source: str):
        self._tokens = tokens
        self._source = source
        self._index = 0
        self._depth = 0

    # -- token access ------------------------------------------------------

    @property
    def _current(self) -> _Token | None:
        if self._index < len(self._tokens):
            return self._tokens[self._index]
        return None

    def _advance(self) -> _Token:
        token = self._tokens[self._index]
        self._index += 1
        return token

    def _at(self, *kinds: str) -> bool:
        token = self._current
        return token is not None and token.kind in kinds

    def _end_position(self) -> int:
        return len(self._source)

    # -- grammar -----------------------------------------------------------

    def parse(self) -> Node:
        node = self._parse_or()
        token = self._current
        if token is not None:
            if token.kind == ")":
                raise KeywordQueryError(
                    "Unmatched “)” — there is no “(” for it to close.",
                    token.position,
                )
            # Every other token kind is consumed by the grammar, so reaching
            # here means the expression ended and something followed it.
            raise KeywordQueryError(f"Unexpected “{token.text}” here.", token.position)
        return node

    def _parse_or(self) -> Node:
        operands = [self._parse_and()]
        while self._at("or"):
            operator = self._advance()
            operands.append(self._parse_and(after=operator))
        if len(operands) == 1:
            return operands[0]
        return Or(tuple(operands))

    def _parse_and(self, after: _Token | None = None) -> Node:
        operands = [self._parse_unary(after=after)]
        while True:
            if self._at("and"):
                operator = self._advance()
                operands.append(self._parse_unary(after=operator))
                continue
            # Adjacency is AND: `climate change` is both words, which is what
            # somebody typing two words into a search box means.
            if self._at("term", "phrase", "code", "not", "scope", "("):
                operands.append(self._parse_unary())
                continue
            break
        if len(operands) == 1:
            return operands[0]
        return And(tuple(operands))

    def _parse_unary(self, after: _Token | None = None) -> Node:
        if self._at("not"):
            operator = self._advance()
            return Not(self._parse_unary(after=operator))
        if self._at("scope"):
            prefix = self._advance()
            # Distributed over whatever follows, so `q:(a OR b)` is
            # `q:a OR q:b` -- the way a prefix behaves in every other search box.
            # A scope written further in wins, so `q:(a OR a:b)` still looks for
            # b in the answer: the nearer one is the more deliberate.
            assert prefix.scope is not None
            return _scoped(self._parse_unary(after=prefix), prefix.scope)
        return self._parse_primary(after=after)

    def _parse_primary(self, after: _Token | None = None) -> Node:
        token = self._current

        if token is None:
            if after is not None:
                raise KeywordQueryError(
                    f"“{after.text}” needs something after it.", after.position
                )
            raise KeywordQueryError("The query is empty.", self._end_position())

        if token.kind == "(":
            self._advance()
            self._depth += 1
            if self._depth > MAX_DEPTH:
                raise KeywordQueryError(
                    f"Too many nested brackets (at most {MAX_DEPTH}).",
                    token.position,
                )
            if self._at(")"):
                raise KeywordQueryError(
                    "Empty brackets — put something between “(” and “)”.",
                    token.position,
                )
            node = self._parse_or()
            if not self._at(")"):
                raise KeywordQueryError(
                    "Unclosed “(” — add a closing bracket.", token.position
                )
            self._advance()
            self._depth -= 1
            return node

        if token.kind == "phrase":
            self._advance()
            return _phrase_term(token)

        if token.kind == "term":
            self._advance()
            return _bare_term(token)

        if token.kind == "code":
            self._advance()
            assert token.path is not None
            return CodeTerm(
                path=token.path, subtree=token.subtree, position=token.position
            )

        # An operator where a term was expected: `AND dog`, `dog OR OR cat`.
        if after is not None:
            raise KeywordQueryError(
                f"“{after.text}” needs something after it, not “{token.text}”.",
                after.position,
            )
        raise KeywordQueryError(
            f"“{token.text}” needs something before it.", token.position
        )


def _scoped(node: Node, scope: Scope) -> Node:
    """`node` with `scope` filled in wherever nothing more specific was written.

    Innermost wins, so a term carrying its own `q:`/`a:` keeps it and only the
    terms that said nothing take the outer one.

    Code terms take a *written* prefix like any other leaf -- `a:code:stress` is
    a coding on the answer -- but they never take the caller's default, which is
    applied later and separately. `resolve_scope` says why.
    """
    if isinstance(node, (Term, CodeTerm)):
        return node if node.scope is not None else replace(node, scope=scope)
    if isinstance(node, Not):
        return Not(_scoped(node.operand, scope))
    if isinstance(node, And):
        return And(tuple(_scoped(operand, scope) for operand in node.operands))
    return Or(tuple(_scoped(operand, scope) for operand in node.operands))


def _bare_term(token: _Token) -> Term:
    """A bare word, with its `*` edges taken off and recorded.

    Only the outermost `*` on each side means anything; a `*` in the middle is
    the character itself, because a respondent writing "3*4" wrote a
    multiplication sign and not a wildcard.
    """
    text = token.text
    open_start = text.startswith("*")
    if open_start:
        text = text[1:]
    open_end = text.endswith("*")
    if open_end:
        text = text[:-1]

    if not text:
        raise KeywordQueryError(
            "“*” on its own matches everything — search for a word instead.",
            token.position,
        )
    if len(text) > MAX_TERM_LENGTH:
        raise KeywordQueryError(
            f"“{text[:20]}…” is too long (at most {MAX_TERM_LENGTH} characters).",
            token.position,
        )
    return Term(text=text, open_start=open_start, open_end=open_end, scope=token.scope)


def _phrase_term(token: _Token) -> Term:
    """A quoted phrase. `*` inside quotes is the character, not a wildcard --
    quoting is how a reader asks for the literal thing."""
    if len(token.text) > MAX_TERM_LENGTH:
        raise KeywordQueryError(
            f"“{token.text[:20]}…” is too long (at most {MAX_TERM_LENGTH} characters).",
            token.position,
        )
    return Term(text=token.text, phrase=True)


def parse(query: str) -> Node | None:
    """The query as a tree, or None where it asks for nothing.

    Raises `KeywordQueryError` on anything it cannot read.
    """
    if not query or not query.strip():
        return None

    node = _Parser(_tokenize(query), query).parse()

    count = len(leaves_of(node))
    if count > MAX_TERMS:
        raise KeywordQueryError(
            f"Too many search terms ({count}; at most {MAX_TERMS}).", None
        )
    return node


# --------------------------------------------------------------------------
# Compilation to a regular expression
# --------------------------------------------------------------------------

#: A word boundary written as a lookaround rather than as `\b`, so that it also
#: does the right thing for a term ending in punctuation: `\b` after the `%` of
#: "100%" would demand a word character next, while `(?!\w)` demands only that
#: the match is not glued to one.
#:
#: Lookarounds and `\w` mean the same thing in Python's `re` and in Postgres'
#: regular expressions, which is why they are spelled this way and not with
#: Postgres' `\y` or Python's `\b` -- one pattern has to satisfy both engines.
_OPEN_START = "(?<!\\w)"
_OPEN_END = "(?!\\w)"


def term_pattern(term: Term) -> str:
    """A term as a regular expression, anchored at whichever edges are closed."""
    if term.phrase:
        # Any run of whitespace between the words, so a phrase still matches
        # where the respondent's line wrapped in the middle of it.
        body = "\\s+".join(re.escape(word) for word in term.text.split())
    else:
        body = re.escape(term.text)

    start = "" if term.open_start else _OPEN_START
    end = "" if term.open_end else _OPEN_END
    return f"{start}{body}{end}"


def resolve_scope(term: Term | CodeTerm, default: Scope) -> Scope:
    """The side of the exchange this term is actually matched against.

    The term's own `q:`/`a:` wins; otherwise the caller's default, which is what
    the segmented control sets. Written once so the condition and the
    highlighting cannot disagree about which is which.

    A code term falls back to "both" instead, and never to the default. The
    control governs *words*: it exists because a chunk restates the question it
    answers, so counting the interviewer's phrasing would let the guide read as
    a finding. None of that is true of a coding, which is a deliberate mark
    somebody put on a passage. Taking the default here would mean its ordinary
    setting ("answer") silently dropped every coding made on an interviewer
    turn -- a filter quietly answering a narrower question than it was asked.
    """
    if isinstance(term, CodeTerm):
        return term.scope or "both"
    return term.scope or default


def compile_condition(
    node: Node, answer, question, default: Scope = "answer", code=None
):
    """The tree as a SQLAlchemy condition over an answer and its question.

    `answer` is the respondent's message; `question` is the interviewer turn
    that drew it, which in the message table is the row before. A term scoped to
    "both" is satisfied by either.

    `code` builds the condition for a code term -- given the `CodeTerm`, it
    returns SQL. Passed in rather than built here because placing a name in a
    codebook needs the project, which this module has no business knowing. A
    caller that hands over none refuses a query containing `code:` outright,
    which is the honest failure: compiling it away would return every chunk and
    call it a match.

    `regexp_match` with `flags="i"` renders as `~*` on Postgres. SQLite ignores
    the flag -- it has no regular expressions of its own -- so the `REGEXP`
    function registered in `app.db.regexp` is the one that has to be
    case-insensitive, and is.
    """
    if isinstance(node, CodeTerm):
        if code is None:
            raise KeywordQueryError(
                "Filtering by code is not available here.", node.position
            )
        return code(node)
    if isinstance(node, Term):
        pattern = term_pattern(node)
        scope = resolve_scope(node, default)
        if scope == "answer":
            return answer.regexp_match(pattern, flags="i")
        if scope == "question":
            # A respondent message with no interviewer turn before it has no
            # question to match, and NULL is not a match.
            return and_(
                question.is_not(None), question.regexp_match(pattern, flags="i")
            )
        return or_(
            answer.regexp_match(pattern, flags="i"),
            and_(question.is_not(None), question.regexp_match(pattern, flags="i")),
        )
    if isinstance(node, Not):
        # A message that is NULL matches nothing, negated or not: `NOT dog`
        # asks for messages saying something other than dog, not for the
        # absence of a message.
        return and_(
            answer.is_not(None),
            not_(compile_condition(node.operand, answer, question, default, code)),
        )
    if isinstance(node, And):
        return and_(
            *(
                compile_condition(operand, answer, question, default, code)
                for operand in node.operands
            )
        )
    return or_(
        *(
            compile_condition(operand, answer, question, default, code)
            for operand in node.operands
        )
    )


# --------------------------------------------------------------------------
# Highlighting
# --------------------------------------------------------------------------

#: How far an open edge is allowed to run when marking a match. The database
#: matched a prefix -- `arbejd*` matched "arbejd" inside "arbejdsplads" -- but
#: marking six letters of a word and leaving the rest dark points at a fragment
#: nobody searched for, so the mark runs to the end of the word.
_WORD_RUN = "\\w*"


def _highlight_pattern(term: Term) -> str:
    """One term as something to mark, which is wider than what it matched.

    Both edges end up anchored to word boundaries even where the term opened
    one, so a mark is always whole words. See `_WORD_RUN`.
    """
    if term.phrase:
        body = "\\s+".join(re.escape(word) for word in term.text.split())
    else:
        body = re.escape(term.text)

    before = _WORD_RUN if term.open_start else ""
    after = _WORD_RUN if term.open_end else ""
    return f"{_OPEN_START}{before}{body}{after}{_OPEN_END}"


def _terms_of_polarity(
    node: Node,
    wanted: bool,
    positive: bool = True,
) -> list[Term]:
    """Every term under an even (or odd) number of NOTs.

    `wanted=True` gives the terms whose presence puts a chunk in the results;
    `wanted=False` gives the ones whose presence keeps it out.
    """
    if isinstance(node, Term):
        return [node] if positive == wanted else []
    if isinstance(node, CodeTerm):
        # Nothing to mark: a code term matches no text. See `terms_of`.
        return []
    if isinstance(node, Not):
        return _terms_of_polarity(node.operand, wanted, not positive)
    return [
        term
        for operand in node.operands
        for term in _terms_of_polarity(operand, wanted, positive)
    ]


def _positive_terms(
    node: Node, side: Literal["answer", "question"], default: Scope
) -> list[Term]:
    """The terms that put a chunk here, restricted to one side of the exchange.

    `default` is what a bare term means, and is not the same thing as `side`:
    with the scope set to Answers, a bare term is not looked for in the
    interviewer's question and so must not be marked there either.
    """
    return [
        term
        for term in _terms_of_polarity(node, True)
        if resolve_scope(term, default) in (side, "both")
    ]


#: A tag, as the client's sanitiser reads one.
#:
#: Guide text -- and only guide text -- may carry markup, and the same run of
#: characters is a tag in three places that must agree: here, where the marks
#: are computed; in the SQL that decides whether a row matched at all; and in
#: `frontend/src/lib/utils/sanitize.ts`, which renders it. Written once, in a
#: spelling Python's `re` and Postgres's ARE both read the same way, and kept
#: deliberately close to the sanitiser's own pattern -- quoted attribute values
#: may contain ``>``, which a plain ``<[^>]*>`` would end the tag on.
#:
#: The rule the three share is that a tag-shaped run is markup and not prose.
#: Nobody said ``href``, and a keyword scan happily finds a word inside a URL:
#: on this corpus a search for ``stress*`` matches "Stressand" in a support
#: page's address. Marking that would tell a reader somebody said a word that
#: nobody said, and selecting on it would return a result with nothing in it to
#: see.
MARKUP_PATTERN = "</?[a-zA-Z][a-zA-Z0-9-]*(?:[^>\"']|\"[^\"]*\"|'[^']*')*>"

_MARKUP = re.compile(MARKUP_PATTERN)


def _markup_ranges(text: str) -> list[tuple[int, int]]:
    """Where `text` is tag rather than prose."""
    return [(found.start(), found.end()) for found in _MARKUP.finditer(text)]


def _outside_markup(
    spans: list[tuple[int, int]], text: str, applies: bool
) -> list[tuple[int, int]]:
    """`spans`, less any that overlap a tag.

    `applies` is false for respondent text, which is never rendered as markup:
    a respondent who types ``<b>`` is shown those characters, so a term that
    matched them matched something they can see. Only guide text is markup.
    """
    if not applies:
        return spans
    holes = _markup_ranges(text)
    if not holes:
        return spans
    return [
        span
        for span in spans
        if not any(span[0] < end and start < span[1] for start, end in holes)
    ]


def match_spans(
    text: str, node: Node | None, side: Literal["answer", "question"], default: Scope
) -> list[tuple[int, int]]:
    """Where `text` says what was searched for, as `(start, end)` character
    offsets, for one side of the exchange.

    Computed here rather than in the browser so that the thing which decided a
    row is on screen is the thing that marks it. A client re-deriving the marks
    from the query string would be a second matcher with its own idea of what a
    letter is -- and it would be marking the rendered turn rather than the
    column the database actually matched.

    Offsets rather than marked-up text: the caller escapes what it renders, and
    a string of tags would be the one place that stopped being true.
    """
    if node is None or not text:
        return []

    terms = _positive_terms(node, side, default)
    if not terms:
        return []

    # Longest first, so that where two terms could match at the same place the
    # fuller one wins and the mark is not cut short.
    patterns = sorted(terms, key=lambda term: -len(term.text))
    combined = "|".join(f"(?:{_highlight_pattern(term)})" for term in patterns)

    spans: list[tuple[int, int]] = []
    for found in re.finditer(combined, text, re.IGNORECASE | re.UNICODE):
        if found.end() > found.start():
            spans.append((found.start(), found.end()))
    return _outside_markup(_merged(spans), text, side == "question")


def excluded_spans(
    text: str,
    node: Node | None,
    matched: list[tuple[int, int]],
    side: Literal["answer", "question"] = "answer",
) -> list[tuple[int, int]]:
    """Where `text` says something the query asked *not* to see.

    Not restricted by scope, and deliberately so. A negated term can survive on
    screen in two ways -- in text the scope never searched (the interviewer's
    question, under "Answers"), or in a sibling turn of a grouped chunk, because
    the condition is checked per message and then lifted to the group. Both are
    worth seeing: they are the reason a result can look like it contradicts the
    query it came from. Marking them in their own colour says "present, but not
    what put this here".

    Anything already claimed as a match wins, so no character is marked twice.

    `side` is taken only to know whether this text may be markup, the same
    reason `match_spans` takes it: an excluded word found inside an ``href`` is
    no more said than a matched one.
    """
    if node is None or not text:
        return []

    terms = _terms_of_polarity(node, False)
    if not terms:
        return []

    ordered = sorted(terms, key=lambda term: -len(term.text))
    combined = "|".join(f"(?:{_highlight_pattern(term)})" for term in ordered)

    spans = [
        (found.start(), found.end())
        for found in re.finditer(combined, text, re.IGNORECASE | re.UNICODE)
        if found.end() > found.start()
    ]
    kept = [
        span
        for span in _merged(spans)
        if not any(span[0] < end and start < span[1] for start, end in matched)
    ]
    return _outside_markup(kept, text, side == "question")


def _merged(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Overlapping or touching spans joined, so a renderer can walk them in one
    pass without nesting one mark inside another."""
    if not spans:
        return []
    ordered = sorted(spans)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged
