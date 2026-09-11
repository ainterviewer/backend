"""relabel survey answers stored as free text

An interview that reconnected while a survey item was on screen replayed its
history and then waited for the answer. The wait went through
`AInterviewer.receive_data` with no message type, and the IO layer cannot work
one out for itself -- the respondent's client submits a closed answer in the
same frame as free text -- so the row landed as TEXT. The library now derives
the type from the turn the answer lands on; this repairs the rows written
before it did.

The rows are found by the pairing `app.db.survey_answers` and the browse query
already use: a survey item is a snapshot on the *question*, and the answer is
the respondent message after it. A respondent turn whose preceding message is
an interviewer turn carrying a survey item answered that item, whatever its
stored type says.

The answers themselves were never wrong, so nothing user-facing changes.
`message_type` is what `DefaultChunkPolicy.should_embed_message` and its SQL
mirror `EmbeddingRepository._embeddable_conditions` gate on, though, which left
these rows embedded and keyword-searchable as if a respondent had written them
while every correctly labelled sibling was excluded. Survey reports and cohort
filters read the question's survey item instead and were right throughout.

Spoken answers are left alone. The live path does label an audio answer to a
survey item as SURVEY_ITEM, but the recording is still referenced by
`audio_file`, and rewriting those rows is a judgement about the data rather
than a repair of this bug.

Embeddings: only MESSAGE chunks of the relabelled rows are dropped, and they
are dropped rather than re-derived -- under the policy they should never have
existed. QA-pair, section and interview chunks are untouched, because what goes
into those is decided by `Turn.is_free_text`, which reads the survey item off
the question and so was right all along. No backfill run is needed afterwards.

Irreversible: once relabelled, a repaired row is indistinguishable from one the
live path wrote correctly.

Revision ID: a3d7c05e91b4
Revises: e2f7b1c30d94
Create Date: 2026-09-11 12:00:00.000000

"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3d7c05e91b4"
down_revision: str | None = "e2f7b1c30d94"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

# `SQLEnum` stores the enum member's *name*, not its value, so these are the
# strings actually in the column.
TEXT = "TEXT"
SURVEY_ITEM = "SURVEY_ITEM"
USER = "USER"
ASSISTANT = "ASSISTANT"
MESSAGE = "MESSAGE"

# The window runs over every message before any filtering: whether a respondent
# turn answered a survey item depends on the row *before* it, and filtering
# first would make "the row before" whichever row happened to survive. Two flags
# are lagged rather than the columns themselves, because `lag` returns the raw
# stored value and skips the column type's decoding -- the JSONB would arrive as
# the string "null" and compare equal to nothing. Same construction as
# `EmbeddingRepository._message_source`, for the same reasons.
CANDIDATES = f"""
WITH ordered AS (
    SELECT
        id,
        role,
        message_type,
        LAG(CASE
            WHEN survey_item IS NOT NULL AND CAST(survey_item AS TEXT) <> 'null'
            THEN 1 ELSE 0
        END) OVER (PARTITION BY interview_id ORDER BY message_id)
            AS question_had_survey_item,
        LAG(CASE WHEN role = '{ASSISTANT}' THEN 1 ELSE 0 END)
            OVER (PARTITION BY interview_id ORDER BY message_id)
            AS previous_was_interviewer
    FROM message
)
SELECT id FROM ordered
WHERE role = '{USER}'
  AND message_type = '{TEXT}'
  AND question_had_survey_item = 1
  AND previous_was_interviewer = 1
"""


def upgrade() -> None:
    bind = op.get_bind()

    ids = [row[0] for row in bind.execute(sa.text(CANDIDATES))]

    if not ids:
        logger.info("no mislabelled survey answers found")
        return

    # Dropped before the relabel: afterwards the rows no longer look embeddable,
    # and the chunks would be orphaned with nothing left to find them by.
    deleted = bind.execute(
        sa.text(
            f"DELETE FROM embedding WHERE kind = '{MESSAGE}' AND message_id IN :ids"
        ).bindparams(sa.bindparam("ids", value=tuple(ids), expanding=True))
    ).rowcount

    relabelled = bind.execute(
        sa.text(
            f"UPDATE message SET message_type = '{SURVEY_ITEM}' WHERE id IN :ids"
        ).bindparams(sa.bindparam("ids", value=tuple(ids), expanding=True))
    ).rowcount

    logger.info(
        "relabelled %d survey answer(s) stored as free text; "
        "dropped %d message embedding(s)",
        relabelled,
        deleted,
    )


def downgrade() -> None:
    # Irreversible: a repaired row is indistinguishable from one the live path
    # labelled correctly, and the dropped embeddings should not exist under the
    # chunk policy either way.
    pass
