"""The codebook, the codings made with it and comments on messages."""

from __future__ import annotations

from datetime import datetime

from pydantic import UUID4, AliasChoices, Field

from ..types import CodeKind
from .common import Schema
from .users import AuthorPublic


class CodeInput(Schema):
    """One code as the client sends it, id and all.

    The id is the client's because a code is dragged, renamed and coded with
    long before the codebook is saved; see ``CodeTable``.
    """

    id: UUID4
    parent_id: UUID4 | None = None
    name: str = ""
    definition: str = ""
    memo: str = ""
    color: str = ""
    kind: CodeKind = CodeKind.TAG
    min_value: int | None = None
    max_value: int | None = None
    position_x: float | None = None
    position_y: float | None = None


class CodePublic(CodeInput):
    created_at: datetime
    updated_at: datetime


class CodebookReplace(Schema):
    """A whole codebook, replacing the stored one.

    The codebook is edited as one document -- a drag re-parents a branch and
    reorders two sets of siblings at once, and the editor holds an undo stack
    over the whole thing -- so it is saved as one, and ``codes`` is authoritative:
    a code the client leaves out is deleted, along with every coding made with
    it. List order is sibling order.

    ``merges`` is the one exception to "left out means deleted with its
    codings": it maps a code the client has merged away to the code that
    absorbed it, and the merged code's codings are moved there rather than
    dropped. It rides along with the document instead of being an endpoint of
    its own so that a merge is an edit like any other -- undoable until saved,
    and never racing the unsaved edits around it. An entry whose code is
    already gone from the store is a merge an earlier save carried out, and is
    ignored.
    """

    codes: list[CodeInput]
    palette: list[str]
    merges: dict[UUID4, UUID4] = {}


class CodebookPublic(Schema):
    """The stored codebook, in the order the tree reads."""

    codes: list[CodePublic]
    palette: list[str]


class FilteredMessagesRequest(Schema):
    code_ids: list[UUID4] | None = None
    search_text: str | None = None
    exact_match: bool = False
    case_sensitive: bool = False
    questions: list[tuple[int, int]] | None = None
    include_previous_on_user: bool = True


#: How many messages one codings lookup may name. A page of explore results is
#: tens of turns; this is room for several pages and a bound on the query.
MAX_CODING_LOOKUP = 500


class CodingBase(Schema):
    """One passage coded with one code.

    ``start_offset``/``end_offset`` are character offsets into the message's
    content, or both NULL for the whole message. ``value_int`` carries a
    score's number and is NULL on a tag.
    """

    code_id: UUID4
    start_offset: int | None = None
    end_offset: int | None = None
    value_int: int | None = None


class CodingCreate(CodingBase):
    pass


class CodingLookupRequest(Schema):
    """Which messages to read the codings of.

    A POST for a read, like the filtered-message endpoints beside it: the
    explore page draws a page of results as a mosaic of turns from many
    interviews, so the ask is a few hundred message ids -- more than belongs in
    a query string, and a request per turn would be a request per turn.
    """

    message_ids: list[UUID4] = Field(max_length=MAX_CODING_LOOKUP)


class CodingPublic(CodingBase):
    id: UUID4
    message_id: UUID4
    user_id: UUID4
    created_at: datetime
    updated_at: datetime
    # The ORM relationship is called ``user``; the payload calls it ``author``.
    author: AuthorPublic = Field(validation_alias=AliasChoices("author", "user"))


class MessageCommentCreate(Schema):
    """A new comment. The author is taken from the caller's token, never from
    the payload; ``parent_id`` must name a root comment on the same message."""

    body: str = Field(min_length=1)
    parent_id: UUID4 | None = None


class MessageCommentUpdate(Schema):
    body: str = Field(min_length=1)


class MessageCommentPublic(Schema):
    id: UUID4
    message_id: UUID4
    user_id: UUID4
    parent_id: UUID4 | None
    body: str
    created_at: datetime
    updated_at: datetime
    # The ORM relationship is called ``user``; the payload calls it ``author``.
    author: AuthorPublic = Field(validation_alias=AliasChoices("author", "user"))
    # Only ever populated on a root comment: threads are two levels deep.
    replies: list[MessageCommentPublic] = []
