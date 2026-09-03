"""unescape html entities in message content

Inbound message content used to be run through `html.escape` by a validator on
`ReceivedData` (`ainterviewer.interfaces`), so what landed in the database was
`I can&#x27;t remember` rather than the sentence the respondent typed. The
escaping was invisible in the chat -- the frontend rendered message text through
`{@html}`, which decoded it again -- but everything else saw the entities: agent
prompts, embeddings, exports and every dashboard view that renders text as text.

The validator is gone; the XSS defense now lives at the render site, where the
context is actually known. This repairs the rows written while it was in place.

Two details:

* Rows can be escaped more than once (`&amp;#x27;`, even `&amp;amp;`). The
  synthetic interview client built a `ReceivedData` before sending, and the
  server parsed the payload back into that same model, so the validator ran
  twice on those answers (fixed in `app/synthesize/core.py`). The repair
  therefore iterates to a fixed point rather than unescaping once.
* Only the five entities `html.escape` produces are decoded, in the order that
  inverts it (`&amp;` last). Using `html.unescape` here would also decode
  entities a respondent may have typed themselves.

Embeddings of repaired interviews are dropped so the next backfill re-derives
them. It could not find them on its own: a stored vector is only reconsidered
when its `content_hash` or `format_version` changes, and repairing the source
text changes neither for a chunk that was already embedded from the escaped
string. Run the backfill after upgrading:

    uv run python -m app.embed.cli backfill

Irreversible: the downgrade cannot tell repaired text from text that always
contained those characters, and re-escaping would corrupt the latter.

Revision ID: e2f7b1c30d94
Revises: 8433cc9ebf8e
Create Date: 2026-09-03 12:00:00.000000

"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2f7b1c30d94"
down_revision: str | None = "8433cc9ebf8e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

# The exact inverse of `html.escape(quote=True)`, in the order that inverts it:
# `&amp;` must come last or `&amp;lt;` would decode straight through to `<`.
_ENTITIES: tuple[tuple[str, str], ...] = (
    ("&lt;", "<"),
    ("&gt;", ">"),
    ("&quot;", '"'),
    ("&#x27;", "'"),
    ("&amp;", "&"),
)

# Enough passes to clear the deepest nesting observed (three) with room to
# spare, while still terminating on adversarial input.
_MAX_PASSES = 8


def _unescape(text: str) -> str:
    """Decode escaped entities repeatedly until the text stops changing."""
    for _ in range(_MAX_PASSES):
        decoded = text
        for entity, char in _ENTITIES:
            decoded = decoded.replace(entity, char)
        if decoded == text:
            return text
        text = decoded
    return text


def upgrade() -> None:
    bind = op.get_bind()

    predicate = " OR ".join(f"content LIKE '%{entity}%'" for entity, _ in _ENTITIES)
    rows = bind.execute(
        sa.text(f"SELECT id, interview_id, content FROM message WHERE {predicate}")
    ).fetchall()

    if not rows:
        logger.info("no escaped message content found")
        return

    repaired = 0
    interview_ids: set[str] = set()
    for message_id, interview_id, content in rows:
        decoded = _unescape(content)
        if decoded == content:
            continue
        bind.execute(
            sa.text("UPDATE message SET content = :content WHERE id = :id"),
            {"content": decoded, "id": message_id},
        )
        repaired += 1
        if interview_id is not None:
            interview_ids.add(interview_id)

    logger.info(
        "unescaped %d message rows across %d interviews", repaired, len(interview_ids)
    )

    if not interview_ids:
        return

    # Scoped by interview rather than by matching the entities in
    # `embedding.text`: a QA-pair chunk spans several messages, and dropping a
    # few extra chunks only costs the backfill some inference.
    deleted = bind.execute(
        sa.text("DELETE FROM embedding WHERE interview_id IN :ids").bindparams(
            sa.bindparam("ids", value=tuple(interview_ids), expanding=True)
        )
    ).rowcount
    logger.info("dropped %d embeddings for re-derivation; run the backfill", deleted)


def downgrade() -> None:
    # Irreversible: re-escaping would corrupt text that legitimately contains
    # these characters, and the repaired rows are indistinguishable from those.
    pass
