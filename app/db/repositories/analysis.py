from pydantic import UUID4
from sqlalchemy import delete, distinct, exists, func, or_, select, update
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import joinedload, selectinload

from ainterviewer.types import MessageRole
from ainterviewer.utils import now

from ...types import Scope
from ..models import (
    CodeBase,
    CodebookPublic,
    CodebookPut,
    CodePublic,
    CodingCreate,
    CodingPublic,
    MessageCommentCreate,
    MessageCommentPublic,
    MessagePublic,
)
from ..tables import (
    CodeTable,
    CodingTable,
    MessageCommentTable,
    MessageTable,
    ProjectTable,
)
from ..types import DEFAULT_PALETTE, CodeKind
from .base import BaseRepository
from .errors import CodebookError, CodingError, CommentThreadError
from .permissions import can_moderate_project


class AnalysisRepository(BaseRepository):
    """Repository for the codebook, the codings made with it, and comments.

    Everything here takes the project it acts in and filters on it. A message,
    a code, a coding and a comment all have ids of their own, and an endpoint
    that took one on its word could be handed an id from a project the caller
    has no business reading -- so the project is part of the query rather than
    something checked beside it, and an id from elsewhere is simply not found.
    See `app/api/dashboard/analysis` for the role check that runs first.
    """

    # ==================== Scoping ====================

    def _require_message(self, project_id: UUID4, message_id: UUID4) -> None:
        """Raise unless the message is one of this project's."""
        found = self.session.execute(
            select(MessageTable.id).where(
                MessageTable.id == message_id,
                MessageTable.project_id == project_id,
            )
        ).scalar_one_or_none()
        if found is None:
            raise NoResultFound("Message not found")

    def _scoped_coding(self, project_id: UUID4, coding_id: UUID4) -> CodingTable:
        coding = self.session.execute(
            select(CodingTable)
            .join(MessageTable, MessageTable.id == CodingTable.message_id)
            .where(
                CodingTable.id == coding_id,
                MessageTable.project_id == project_id,
            )
        ).scalar_one_or_none()
        if coding is None:
            raise NoResultFound("Coding not found")
        return coding

    def _scoped_comment(
        self, project_id: UUID4, comment_id: UUID4
    ) -> MessageCommentTable:
        comment = self.session.execute(
            select(MessageCommentTable)
            .join(MessageTable, MessageTable.id == MessageCommentTable.message_id)
            .where(
                MessageCommentTable.id == comment_id,
                MessageTable.project_id == project_id,
            )
        ).scalar_one_or_none()
        if comment is None:
            raise NoResultFound("Comment not found")
        return comment

    def _scoped_code(self, project_id: UUID4, code_id: UUID4) -> CodeTable:
        code = self.session.execute(
            select(CodeTable).where(
                CodeTable.id == code_id,
                CodeTable.project_id == project_id,
            )
        ).scalar_one_or_none()
        if code is None:
            raise NoResultFound("Code not found")
        return code

    # ==================== Codebook Methods ====================

    def _project_codes(self, project_id: UUID4) -> list[CodeTable]:
        return list(
            self.session.execute(
                select(CodeTable).where(CodeTable.project_id == project_id)
            )
            .scalars()
            .all()
        )

    @staticmethod
    def _outline(codes: list[CodeTable]) -> list[CodeTable]:
        """The codes depth-first: every code followed by its own branch.

        The stored rows carry `parent_id` and `rank` and no order of their own,
        so the reading order is rebuilt here rather than asked of SQL, which
        cannot express "a parent, then its children, then the next parent"
        without a recursive CTE for something a codebook-sized list does in
        microseconds. Clients rely on it: the frontend's array order *is* its
        sibling order.
        """
        children: dict[UUID4 | None, list[CodeTable]] = {}
        for code in codes:
            children.setdefault(code.parent_id, []).append(code)
        for siblings in children.values():
            siblings.sort(key=lambda code: (code.rank, code.created_at))

        outline: list[CodeTable] = []

        def walk(parent_id: UUID4 | None) -> None:
            for code in children.get(parent_id, []):
                outline.append(code)
                walk(code.id)

        walk(None)
        # A code orphaned by a parent that is gone would otherwise vanish from
        # the codebook while still owning codings. Nothing should produce one
        # -- `save_codebook` refuses a dangling parent -- so this is a floor,
        # not a feature: surface it at the top level and let it be re-parented.
        seen = {code.id for code in outline}
        outline.extend(code for code in codes if code.id not in seen)
        return outline

    def get_codebook(self, project_id: UUID4) -> CodebookPublic:
        codes = self._outline(self._project_codes(project_id))
        palette = self.session.execute(
            select(ProjectTable.codebook_palette).where(ProjectTable.id == project_id)
        ).scalar_one_or_none()
        return CodebookPublic(
            codes=[CodePublic.model_validate(code) for code in codes],
            palette=list(palette) if palette else list(DEFAULT_PALETTE),
        )

    def save_codebook(self, project_id: UUID4, codebook: CodebookPut) -> CodebookPublic:
        """Replace the project's codebook with the one sent.

        Wholesale rather than per-code because that is how it is edited: one
        drag re-parents a branch and reseats two sets of siblings, and the
        editor holds an undo stack over the whole document. Sending the parts
        would make every intermediate state a thing the server could be left
        in.

        Codes carry client-supplied ids, so this is a diff and not a rewrite:
        a code that survives keeps its row and therefore its codings. A code
        left out is deleted along with its codings, which is what deleting a
        code means -- but *turning* a coded code into a group is refused,
        because dropping somebody's coding is a decision for them rather than
        a side effect of a kind change.
        """
        sent = codebook.codes
        ids = [code.id for code in sent]
        if len(set(ids)) != len(ids):
            raise CodebookError("The codebook contains the same code twice")

        known = set(ids)
        for code in sent:
            if code.parent_id is not None and code.parent_id not in known:
                raise CodebookError(
                    f"'{code.name}' has a parent that is not in the codebook"
                )
            if code.id == code.parent_id:
                raise CodebookError(f"'{code.name}' cannot be its own parent")

        parents = {code.id: code.parent_id for code in sent}
        for code in sent:
            seen: set[UUID4] = {code.id}
            cursor = code.parent_id
            while cursor is not None:
                if cursor in seen:
                    raise CodebookError(f"'{code.name}' sits inside its own branch")
                seen.add(cursor)
                cursor = parents[cursor]

        existing = {code.id: code for code in self._project_codes(project_id)}
        gone = set(existing) - known
        coded = self._coding_counts(project_id)

        for code in sent:
            was = existing.get(code.id)
            if code.kind is CodeKind.GROUP and coded.get(code.id):
                raise CodebookError(
                    f"'{code.name}' is used to code {coded[code.id]} passage(s), "
                    "so it cannot become a group. Remove the codings first."
                )
            if was is None:
                self.session.add(
                    CodeTable(
                        id=code.id,
                        project_id=project_id,
                        **self._code_values(code, sent),
                    )
                )
                continue
            for field, value in self._code_values(code, sent).items():
                setattr(was, field, value)

        if gone:
            # Explicitly, and children first: SQLite does not enforce the
            # foreign keys these rows declare (see CLAUDE.md), so neither
            # cascade fires on its own.
            self.session.execute(
                delete(CodingTable).where(CodingTable.code_id.in_(gone))
            )
            self.session.execute(delete(CodeTable).where(CodeTable.id.in_(gone)))

        self.session.execute(
            update(ProjectTable)
            .where(ProjectTable.id == project_id)
            .values(codebook_palette=list(codebook.palette))
        )
        self.session.commit()
        return self.get_codebook(project_id)

    @staticmethod
    def _code_values(code: CodeBase, sent: list[CodeBase]) -> dict:
        """One code's columns, with its seat among its siblings worked out.

        `rank` is not sent: the client's list order *is* the sibling order, so
        deriving it here is what stops a stored rank from disagreeing with the
        order the codes arrived in.
        """
        siblings = [other.id for other in sent if other.parent_id == code.parent_id]
        scored = code.kind is CodeKind.SCORE
        return {
            "parent_id": code.parent_id,
            "name": code.name,
            "definition": code.definition,
            "memo": code.memo,
            "color": code.color,
            "kind": code.kind,
            # A range on anything but a score is a number nothing reads, and
            # one that outlives a kind change reappears if the code is made a
            # score again -- with the scale it had before somebody changed it.
            "min_value": code.min_value if scored else None,
            "max_value": code.max_value if scored else None,
            "position_x": code.position_x,
            "position_y": code.position_y,
            "rank": siblings.index(code.id),
        }

    def _coding_counts(self, project_id: UUID4) -> dict[UUID4, int]:
        rows = self.session.execute(
            select(CodingTable.code_id, func.count(CodingTable.id))
            .join(CodeTable, CodeTable.id == CodingTable.code_id)
            .where(CodeTable.project_id == project_id)
            .group_by(CodingTable.code_id)
        ).all()
        return {code_id: count for code_id, count in rows}

    def count_codings_by_code(self, project_id: UUID4) -> dict[UUID4, int]:
        """How many passages each code has been applied to.

        Only the code's own codings: a parent does not inherit its children's,
        because a passage coded `Cost` is not thereby coded `Barriers`. That is
        also why `GROUP` exists -- see `CodeKind`.
        """
        return self._coding_counts(project_id)

    def _apply_search_filter(
        self,
        statement,
        search_text: str | None,
        exact_match: bool,
        case_sensitive: bool,
    ):
        if search_text:
            if exact_match:
                if case_sensitive:
                    return statement.where(MessageTable.content == search_text)
                else:
                    return statement.where(
                        func.lower(MessageTable.content) == search_text.lower()
                    )
            else:
                if case_sensitive:
                    return statement.where(
                        MessageTable.content.like(f"%{search_text}%")
                    )
                else:
                    return statement.where(
                        MessageTable.content.ilike(f"%{search_text}%")
                    )
        return statement

    def _apply_questions_filter(
        self,
        statement,
        questions: list[tuple[int, int]] | None,
    ):
        if questions:
            question_filters = [
                (MessageTable.section == section)
                & (MessageTable.main_question == main_question)
                for section, main_question in questions
            ]
            if question_filters:
                return statement.where(or_(*question_filters))
        return statement

    def _load_context(
        self,
        statement,
        context_before: bool,
        context_after: bool,
        include_previous_on_user: bool = False,
    ):
        """Fetches related messages, context_before returns all messages
        to and with the previous main_question and after_context returns all
        messages up to the next main question"""

        if not context_before and not context_after and not include_previous_on_user:
            return statement

        # Convert current statement to a subquery to get matched messages
        matched_messages = statement.subquery("matched")

        # Build conditions for the expanded query
        conditions: list = [
            # Include all originally matched messages
            MessageTable.id.in_(select(matched_messages.c.id))
        ]

        if include_previous_on_user:
            # Include previous message if current message is from user
            conditions.append(
                exists(
                    select(1)
                    .select_from(matched_messages)
                    .where(
                        (matched_messages.c.role == MessageRole.USER)
                        & (MessageTable.project_id == matched_messages.c.project_id)
                        & (MessageTable.interview_id == matched_messages.c.interview_id)
                        & (MessageTable.message_id == matched_messages.c.message_id - 1)
                    )
                )
            )

        if context_before:
            # Include messages from previous main_question
            conditions.append(
                exists(
                    select(1)
                    .select_from(matched_messages)
                    .where(
                        (MessageTable.project_id == matched_messages.c.project_id)
                        & (MessageTable.section == matched_messages.c.section)
                        & (
                            MessageTable.main_question
                            == matched_messages.c.main_question - 1
                        )
                    )
                )
            )

        if context_after:
            # Include messages from next main_question
            conditions.append(
                exists(
                    select(1)
                    .select_from(matched_messages)
                    .where(
                        (MessageTable.project_id == matched_messages.c.project_id)
                        & (MessageTable.section == matched_messages.c.section)
                        & (
                            MessageTable.main_question
                            == matched_messages.c.main_question + 1
                        )
                    )
                )
            )

        # Return new statement with all conditions
        return select(MessageTable).where(or_(*conditions))

    def count_filtered_messages(
        self,
        project_id: UUID4,
        code_ids: list[UUID4] | None = None,
        search_text: str | None = None,
        exact_match: bool = False,
        case_sensitive: bool = False,
        questions: list[tuple[int, int]] | None = None,
    ) -> int:
        statement = select(func.count(distinct(MessageTable.id))).where(
            MessageTable.project_id == project_id
        )

        if code_ids is not None:
            statement = statement.join(MessageTable.codings).where(
                CodingTable.code_id.in_(code_ids)
            )

        statement = self._apply_search_filter(
            statement, search_text, exact_match, case_sensitive
        )
        statement = self._apply_questions_filter(statement, questions)
        return self.session.execute(statement).scalar_one()

    def get_filtered_messages(
        self,
        project_id: UUID4,
        skip: int,
        limit: int,
        context_before: bool = False,
        context_after: bool = False,
        include_previous_on_user: bool = False,
        code_ids: list[UUID4] | None = None,
        search_text: str | None = None,
        exact_match: bool = False,
        case_sensitive: bool = False,
        questions: list[tuple[int, int]] | None = None,
    ) -> list[MessagePublic]:
        statement = select(MessageTable).where(MessageTable.project_id == project_id)

        if code_ids:
            statement = statement.join(MessageTable.codings).where(
                CodingTable.code_id.in_(code_ids)
            )

        statement = self._apply_search_filter(
            statement, search_text, exact_match, case_sensitive
        )
        statement = self._apply_questions_filter(statement, questions)
        statement = self._load_context(
            statement, context_before, context_after, include_previous_on_user
        )

        statement = (
            statement.distinct()
            .offset(skip)
            .limit(limit)
            .options(
                selectinload(MessageTable.codings).joinedload(CodingTable.user),
                selectinload(MessageTable.comments).joinedload(
                    MessageCommentTable.user
                ),
                selectinload(MessageTable.comments)
                .selectinload(MessageCommentTable.replies)
                .joinedload(MessageCommentTable.user),
                selectinload(MessageTable.interview),
            )
        )
        messages = self.session.execute(statement).scalars().all()
        return [MessagePublic.model_validate(message) for message in messages]

    def get_message_context(
        self,
        project_id: UUID4,
        interview_id: UUID4,
        message_id: UUID4,
        context_before: bool = False,
        context_after: bool = False,
    ) -> list[MessagePublic]:
        # First, get the target message to determine its section, main_question, and timestamp
        target_statement = select(MessageTable).where(
            MessageTable.project_id == project_id,
            MessageTable.interview_id == interview_id,
            MessageTable.id == message_id,
        )
        target_message = self.session.execute(target_statement).scalar_one()

        # Build base conditions
        conditions = [
            MessageTable.project_id == project_id,
            MessageTable.interview_id == interview_id,
            MessageTable.section == target_message.section,
            MessageTable.main_question == target_message.main_question,
        ]

        # Add time-based filtering based on context flags
        if not context_before and not context_after:
            # Just return the target message
            conditions.append(MessageTable.id == message_id)
        elif context_before and context_after:
            # Return all messages in the current main_question except the target
            conditions.append(MessageTable.id != message_id)
        elif context_before:
            # Messages before (not including) the target message
            conditions.append(MessageTable.created_at < target_message.created_at)
        elif context_after:
            # Messages after (not including) the target message
            conditions.append(MessageTable.created_at > target_message.created_at)

        # Query for messages
        statement = (
            select(MessageTable)
            .where(*conditions)
            .order_by(MessageTable.created_at)
            .options(
                selectinload(MessageTable.codings).joinedload(CodingTable.user),
                selectinload(MessageTable.comments).joinedload(
                    MessageCommentTable.user
                ),
                selectinload(MessageTable.comments)
                .selectinload(MessageCommentTable.replies)
                .joinedload(MessageCommentTable.user),
                selectinload(MessageTable.interview),
            )
        )
        messages = self.session.execute(statement).scalars().all()

        return [MessagePublic.model_validate(message) for message in messages]

    # ==================== Coding Methods ====================

    def get_message_codings(
        self, project_id: UUID4, message_id: UUID4
    ) -> list[CodingPublic]:
        self._require_message(project_id, message_id)
        statement = (
            select(CodingTable)
            .where(CodingTable.message_id == message_id)
            .order_by(CodingTable.created_at)
            .options(joinedload(CodingTable.user))
        )
        codings = self.session.execute(statement).scalars().all()
        return [CodingPublic.model_validate(coding) for coding in codings]

    def get_codings_for_messages(
        self, project_id: UUID4, message_ids: list[UUID4]
    ) -> dict[UUID4, list[CodingPublic]]:
        """Every coding on each of these messages, keyed by message.

        Messages from another project are simply absent from the result rather
        than an error: the caller is naming what is on its screen, and a screen
        that has drifted from what it may read should lose the codings, not the
        page.

        Messages with no codings are left out too, so the common case -- a page
        of results nobody has coded yet -- is an empty object rather than a
        hundred empty lists.
        """
        if not message_ids:
            return {}

        statement = (
            select(CodingTable)
            .join(MessageTable, MessageTable.id == CodingTable.message_id)
            .where(
                CodingTable.message_id.in_(set(message_ids)),
                MessageTable.project_id == project_id,
            )
            .order_by(CodingTable.created_at)
            .options(joinedload(CodingTable.user))
        )

        found: dict[UUID4, list[CodingPublic]] = {}
        for coding in self.session.execute(statement).scalars().all():
            found.setdefault(coding.message_id, []).append(
                CodingPublic.model_validate(coding)
            )
        return found

    def _check_coding(
        self,
        project_id: UUID4,
        message_id: UUID4,
        coding: CodingCreate,
    ) -> None:
        """Everything that makes a coding meaningful, checked in one place.

        A coding is a claim about a passage, and each of these failures makes
        it a claim about nothing: a group code that is never applied, a score
        with no number or one off its own scale, a span that does not point at
        text in this message. They are 400s rather than silent repairs --
        clamping a span or defaulting a score would store a claim the coder
        did not make.
        """
        try:
            code = self._scoped_code(project_id, coding.code_id)
        except NoResultFound:
            raise CodingError("That code is not in this project's codebook")

        if code.kind is CodeKind.GROUP:
            raise CodingError(
                f"'{code.name}' is a group: it organises the codebook and is "
                "not applied to passages"
            )

        if code.kind is CodeKind.SCORE:
            if coding.value_int is None:
                raise CodingError(f"'{code.name}' is a score and needs a value")
            low = code.min_value
            high = code.max_value
            if (
                low is not None
                and high is not None
                and not low <= coding.value_int <= high
            ):
                raise CodingError(
                    f"{coding.value_int} is outside '{code.name}'s scale ({low}-{high})"
                )
        elif coding.value_int is not None:
            raise CodingError(f"'{code.name}' is a tag and takes no value")

        start, stop = coding.start_offset, coding.end_offset
        if (start is None) != (stop is None):
            raise CodingError("A span needs both a start and an end")
        if start is not None and stop is not None:
            length = self.session.execute(
                select(func.length(MessageTable.content)).where(
                    MessageTable.id == message_id
                )
            ).scalar_one()
            if not 0 <= start < stop <= length:
                raise CodingError("That span does not fall inside the message")

    def _require_no_duplicate(
        self,
        message_id: UUID4,
        user_id: UUID4,
        coding: CodingCreate,
        exclude: UUID4 | None = None,
    ) -> None:
        """One coder cannot code one passage with one code twice.

        Not a unique constraint, because the whole-message case is two NULL
        offsets and SQL counts NULLs as distinct from each other -- the one
        case this actually has to catch, since a second click on an already
        applied tag is an ordinary thing for a UI to send.
        """
        statement = select(CodingTable.id).where(
            CodingTable.message_id == message_id,
            CodingTable.user_id == user_id,
            CodingTable.code_id == coding.code_id,
            CodingTable.start_offset.is_not_distinct_from(coding.start_offset),
            CodingTable.end_offset.is_not_distinct_from(coding.end_offset),
        )
        if exclude is not None:
            statement = statement.where(CodingTable.id != exclude)
        if self.session.execute(statement).scalar_one_or_none() is not None:
            raise CodingError("That passage already carries this code")

    def add_message_coding(
        self,
        project_id: UUID4,
        message_id: UUID4,
        user_id: UUID4,
        coding: CodingCreate,
    ) -> CodingPublic:
        self._require_message(project_id, message_id)
        self._check_coding(project_id, message_id, coding)
        self._require_no_duplicate(message_id, user_id, coding)

        new_coding = CodingTable(
            code_id=coding.code_id,
            message_id=message_id,
            user_id=user_id,
            start_offset=coding.start_offset,
            end_offset=coding.end_offset,
            value_int=coding.value_int,
        )
        self.session.add(new_coding)
        self.session.commit()
        self.session.refresh(new_coding)
        return CodingPublic.model_validate(new_coding)

    def update_message_coding(
        self, project_id: UUID4, coding_id: UUID4, coding: CodingCreate
    ) -> CodingPublic:
        existing = self._scoped_coding(project_id, coding_id)
        self._check_coding(project_id, existing.message_id, coding)
        self._require_no_duplicate(
            existing.message_id, existing.user_id, coding, exclude=coding_id
        )

        existing.code_id = coding.code_id
        existing.start_offset = coding.start_offset
        existing.end_offset = coding.end_offset
        existing.value_int = coding.value_int
        existing.updated_at = now()
        self.session.commit()
        self.session.refresh(existing)
        return CodingPublic.model_validate(existing)

    def can_modify_coding(
        self, project_id: UUID4, coding_id: UUID4, user_id: UUID4, scope: Scope
    ) -> bool:
        """Whether `user_id` may edit or delete this coding.

        Its author always may. Beyond that it takes moderation rights over the
        project -- the same rule comments follow, so that a project keeps a way
        to clean up after a collaborator who has left. Membership itself is the
        endpoint's role check; this decides who among the members may act on
        somebody else's coding.
        """
        coding = self._scoped_coding(project_id, coding_id)
        if coding.user_id == user_id:
            return True

        return can_moderate_project(self.session, user_id, project_id, scope)

    def delete_message_coding(self, project_id: UUID4, coding_id: UUID4) -> None:
        coding = self._scoped_coding(project_id, coding_id)
        self.session.delete(coding)
        self.session.commit()

    # ==================== Message Comment Methods ====================

    def get_message_comments(
        self, project_id: UUID4, message_id: UUID4
    ) -> list[MessageCommentPublic]:
        """The message's discussion: root comments, each carrying its replies."""
        self._require_message(project_id, message_id)
        statement = (
            select(MessageCommentTable)
            .where(
                MessageCommentTable.message_id == message_id,
                MessageCommentTable.parent_id.is_(None),
            )
            .order_by(MessageCommentTable.created_at)
            .options(
                joinedload(MessageCommentTable.user),
                selectinload(MessageCommentTable.replies).joinedload(
                    MessageCommentTable.user
                ),
            )
        )
        comments = self.session.execute(statement).scalars().all()
        return [MessageCommentPublic.model_validate(comment) for comment in comments]

    def add_message_comment(
        self,
        project_id: UUID4,
        message_id: UUID4,
        user_id: UUID4,
        comment: MessageCommentCreate,
    ) -> MessageCommentPublic:
        self._require_message(project_id, message_id)

        if comment.parent_id is not None:
            parent = self.session.get(MessageCommentTable, comment.parent_id)
            if parent is None:
                raise CommentThreadError(
                    "The comment being replied to no longer exists"
                )
            if parent.parent_id is not None:
                raise CommentThreadError(
                    "Replies can only be made to a top-level comment"
                )
            if parent.message_id != message_id:
                raise CommentThreadError(
                    "The comment being replied to is on another message"
                )

        new_comment = MessageCommentTable(
            message_id=message_id,
            user_id=user_id,
            parent_id=comment.parent_id,
            body=comment.body,
        )
        self.session.add(new_comment)
        self.session.commit()
        self.session.refresh(new_comment)
        return MessageCommentPublic.model_validate(new_comment)

    def update_message_comment(
        self, project_id: UUID4, comment_id: UUID4, body: str
    ) -> MessageCommentPublic:
        comment = self._scoped_comment(project_id, comment_id)
        comment.body = body
        self.session.commit()
        self.session.refresh(comment)
        return MessageCommentPublic.model_validate(comment)

    def delete_message_comment(self, project_id: UUID4, comment_id: UUID4) -> None:
        """Delete a comment, and its replies when it is a root.

        The replies are deleted here rather than left to ON DELETE CASCADE:
        SQLite does not enforce foreign keys in this application (see
        CLAUDE.md), so the cascade would leave them orphaned.
        """
        comment = self._scoped_comment(project_id, comment_id)

        if comment.parent_id is None:
            self.session.execute(
                delete(MessageCommentTable).where(
                    MessageCommentTable.parent_id == comment_id
                )
            )
        self.session.delete(comment)
        self.session.commit()

    def can_modify_comment(
        self, project_id: UUID4, comment_id: UUID4, user_id: UUID4, scope: Scope
    ) -> bool:
        """Whether `user_id` may edit or delete this comment.

        Its author always may. Beyond that it takes moderation rights over the
        project -- see `can_moderate_project`. Membership itself is the
        endpoint's role check; this decides who among the members may act on
        somebody else's writing.
        """
        comment = self._scoped_comment(project_id, comment_id)
        if comment.user_id == user_id:
            return True

        return can_moderate_project(self.session, user_id, project_id, scope)
