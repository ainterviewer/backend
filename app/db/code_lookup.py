"""Placing a `code:` reference in a project's codebook.

The parser reads what was *written* -- a name, or a path, and whether `/*` was
on it -- because it has no project to check the name against. This is the half
that does: it loads a project's codes once and turns each reference into the
ids a condition can be built from.

Separate from both sides that need it. The endpoint resolves to find out
whether a query can be answered at all, so that an unplaceable name is a 422
naming the name rather than a 500 or, worse, a filter that quietly matches
nothing. The repository resolves to build the SQL. Neither owns it, so neither
imports the other.

Resolving twice per request is deliberate, for the reason `_checked_keyword`
gives about parsing twice: a caller that never touches the endpoint -- a
script, a test -- gets the same language and the same errors, and the cost is a
single query over a codebook-sized table next to a scan of the whole corpus.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from pydantic import UUID4
from sqlalchemy import select
from sqlalchemy.orm import Session

from .keyword_query import CodeTerm, KeywordQueryError, Node, leaves_of
from .tables import CodeTable
from .types import CodeKind

#: How many candidate paths an ambiguity message lists before it gives up and
#: says how many there are. Long enough to be a menu, short enough to read.
_MAX_SUGGESTIONS = 3


def _normalised(name: str) -> str:
    """A name as it is compared.

    Case-folded rather than lowercased: a codebook written in Danish or German
    has names that `lower()` does not fold the way a reader would expect, and a
    filter that misses "STRAßE" for "straße" is a filter that looks broken.
    """
    return name.strip().casefold()


@dataclass(frozen=True)
class _Code:
    """One code, flattened to what placing a name needs."""

    id: UUID
    parent_id: UUID | None
    name: str
    kind: CodeKind


class CodeIndex:
    """One project's codebook, arranged for looking names up.

    Built once per request and reused for every reference in the query: a
    codebook is small, and a query naming six codes should not be six round
    trips.
    """

    def __init__(self, codes: list[_Code]):
        self._by_id = {code.id: code for code in codes}
        self._by_name: dict[str, list[_Code]] = {}
        self._children: dict[UUID | None, list[_Code]] = {}
        for code in codes:
            self._by_name.setdefault(_normalised(code.name), []).append(code)
            self._children.setdefault(code.parent_id, []).append(code)

    @classmethod
    def for_project(cls, session: Session, project_id: UUID4) -> CodeIndex:
        rows = session.execute(
            select(
                CodeTable.id, CodeTable.parent_id, CodeTable.name, CodeTable.kind
            ).where(CodeTable.project_id == project_id)
        ).all()
        return cls([_Code(*row) for row in rows])

    # -- reading ----------------------------------------------------------

    def path_of(self, code: _Code) -> str:
        """`code` as the path a reader would have to write to name it uniquely
        -- from the root, which is always unambiguous even when a shorter tail
        would have done."""
        steps = [code.name]
        seen = {code.id}
        parent_id = code.parent_id
        # Guarded against a cycle rather than trusted: this runs while building
        # an error message, and a message that hangs is worse than the error.
        while parent_id is not None and parent_id not in seen:
            parent = self._by_id.get(parent_id)
            if parent is None:
                break
            steps.append(parent.name)
            seen.add(parent.id)
            parent_id = parent.parent_id
        return "/".join(reversed(steps))

    def _descendants(self, code: _Code) -> list[_Code]:
        """`code` and everything under it, breadth-first."""
        found = [code]
        index = 0
        seen = {code.id}
        while index < len(found):
            for child in self._children.get(found[index].id, []):
                if child.id not in seen:
                    seen.add(child.id)
                    found.append(child)
            index += 1
        return found

    def ids_under(self, code_id: UUID) -> tuple[UUID, ...]:
        """`code_id` and everything beneath it, or empty if there is no such
        code in this project. What `/*` selects, for a caller that already has
        an id and so has nothing to place."""
        code = self._by_id.get(code_id)
        if code is None:
            return ()
        return tuple(descendant.id for descendant in self._descendants(code))

    def subtrees(self) -> dict[UUID, tuple[UUID, ...]]:
        """Every code in the project, with the ids `/*` on it would select.

        For the side that counts rather than filters: a badge showing what a
        branch is worth has to know the branch, and asking the tree once beats
        walking it per row.
        """
        return {
            code.id: tuple(descendant.id for descendant in self._descendants(code))
            for code in self._by_id.values()
        }

    def _matching(self, path: tuple[str, ...]) -> list[_Code]:
        """Every code the written path could mean.

        The last step is the code itself and each earlier step is its parent, so
        a path is read as a *tail* of ancestors rather than from the root:
        `Stress/Often` is an Often whose parent is a Stress, wherever that sits.
        A reader disambiguating a name should have to add only as much of the
        branch as it takes, not spell the whole way down from the top.
        """
        candidates = self._by_name.get(_normalised(path[-1]), [])
        if len(path) == 1:
            return self._most_specific(path, list(candidates))

        placed = []
        for candidate in candidates:
            code = candidate
            for step in reversed(path[:-1]):
                parent = (
                    self._by_id.get(code.parent_id)
                    if code.parent_id is not None
                    else None
                )
                if parent is None or _normalised(parent.name) != _normalised(step):
                    break
                code = parent
            else:
                placed.append(candidate)
        return self._most_specific(path, placed)

    def _most_specific(self, path: tuple[str, ...], placed: list[_Code]) -> list[_Code]:
        """`placed` narrowed to a full path from the root, where one is written.

        A written path is read as a tail, which leaves one code unnameable
        without this: a root-level "New code" sitting beside a "Stress/New code"
        is matched by every tail its name appears in, so the shortest way to
        name it is also the most ambiguous one -- and the path this would
        *suggest* for it, being the root path, would simply raise the same error
        again. That is a name a reader cannot reach.

        So a path that is somebody's whole descent from the root beats one that
        is merely a tail of it, which is the rule the scope prefixes already
        use: the more specific reading is the more deliberate one. It also
        guarantees the suggestions in an ambiguity message resolve, since those
        are written as full paths.

        Only applied where it settles something. Narrowing a single survivor to
        nothing would turn a name that was found into a name that was not.
        """
        if len(placed) < 2:
            return placed
        written = "/".join(_normalised(step) for step in path)
        exact = [
            code
            for code in placed
            if "/".join(_normalised(step) for step in self.path_of(code).split("/"))
            == written
        ]
        return exact if len(exact) == 1 else placed

    # -- resolving --------------------------------------------------------

    def resolve(self, term: CodeTerm) -> tuple[UUID, ...]:
        """The ids `term` selects, or a `KeywordQueryError` saying why not.

        Raising rather than returning nothing on a name that is not there: a
        reference nobody can place is a query that cannot be answered, and
        answering it with an empty result would tell the reader that nothing in
        the corpus was coded that way -- a different and wrong claim.
        """
        written = "/".join(term.path)
        found = self._matching(term.path)

        if not found:
            raise KeywordQueryError(
                f"No code in this project is called “{written}”.", term.position
            )

        if len(found) > 1:
            raise KeywordQueryError(self._ambiguous(written, found), term.position)

        code = found[0]
        if code.kind == CodeKind.GROUP and not term.subtree:
            # A GROUP organises the branch under it and is never applied, so
            # asking for it exactly is a query that cannot match -- and almost
            # always a reader who meant the branch.
            raise KeywordQueryError(
                f"“{code.name}” only groups the codes under it and is never "
                f'applied on its own — write code:"{self.path_of(code)}"/* for '
                "everything under it.",
                term.position,
            )

        if not term.subtree:
            return (code.id,)
        return tuple(descendant.id for descendant in self._descendants(code))

    def _ambiguous(self, written: str, found: list[_Code]) -> str:
        """Which codes a name could have meant, and how to say which.

        Names the alternatives rather than only the problem: a codebook can
        genuinely hold three codes called "New code", and a reader told only
        that the name is ambiguous still has to go and look.

        Sometimes there is no alternative to offer. Codes that are siblings --
        three "New code"s at the top level, which is what clicking *+ Code*
        three times leaves behind -- have the same path as each other, so every
        suggestion would be the string that just failed. Saying that plainly
        beats offering it three times.
        """
        paths = sorted({self.path_of(code) for code in found})
        if len(paths) < 2:
            return (
                f"{len(found)} codes are called “{written}”, in the same place, "
                "so there is no path that tells them apart — rename one in the "
                "codebook."
            )
        shown = ", ".join(f'code:"{path}"' for path in paths[:_MAX_SUGGESTIONS])
        if len(paths) > _MAX_SUGGESTIONS:
            return (
                f"{len(found)} codes are called “{written}” — write the path "
                f"instead, for example {shown}."
            )
        return (
            f"{len(found)} codes are called “{written}” — write the path "
            f"instead: {shown}."
        )


def resolve_all(session: Session, project_id: UUID4, node: Node) -> None:
    """Place every code reference in `node`, for the side that only wants to know.

    Used where the answer is thrown away and only the error matters -- the
    endpoint, checking that a query can be answered before anything is scanned.
    Returns nothing on purpose: a caller that wants the ids should hold an index
    and resolve against it, rather than be handed a second structure to keep in
    step with the tree.
    """
    terms = [leaf for leaf in leaves_of(node) if isinstance(leaf, CodeTerm)]
    if not terms:
        return
    index = CodeIndex.for_project(session, project_id)
    for term in terms:
        index.resolve(term)
