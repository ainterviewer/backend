"""Turning chunks into stored vectors."""

from ainterviewer.interfaces import EmbeddingChunk

from ..db import InterviewDataBase
from ..db.repositories.embedding import PendingEmbedding
from ..db.types import EmbeddingTask
from .client import EmbeddingClient


class EmbeddingService:
    """Embeds chunks and stores the result.

    Skips whatever is already current: `needs_embedding` compares each chunk's
    text hash against what is stored, so re-running over an interview that has
    not changed costs one query and no inference.
    """

    def __init__(
        self,
        db: InterviewDataBase,
        client: EmbeddingClient,
        task: EmbeddingTask = EmbeddingTask.DOCUMENT,
    ):
        self.db = db
        self.client = client
        self.task = task

    @property
    def model(self) -> str:
        return self.client.settings.model

    async def embed_and_store(
        self,
        chunks: list[EmbeddingChunk],
        *,
        force: bool = False,
        commit: bool = True,
    ) -> int:
        """Embed the chunks that need it and store them. Returns rows written.

        Raises `EmbeddingUnavailable` if the server cannot be reached: callers
        on a background path should log and move on, leaving the work for the
        next backfill.
        """
        if not chunks:
            return 0

        # Truncate *before* the staleness check, not just before sending. The
        # stored `content_hash` describes the text the model actually saw, so
        # comparing it against an untruncated hash can never match: an
        # over-length chunk would look stale on every run and be re-embedded
        # forever. Normalising here keeps one definition of "the text".
        chunks = [
            chunk.model_copy(update={"text": text})
            for chunk in chunks
            if (text := self.client.truncate(chunk.text))
        ]

        outstanding = (
            chunks
            if force
            else self.db.embeddings.needs_embedding(
                chunks, task=self.task, model=self.model
            )
        )
        if not outstanding:
            return 0

        vectors = await self.client.embed_documents(
            [chunk.text for chunk in outstanding]
        )

        pending = [
            PendingEmbedding(chunk=chunk, text=chunk.text, vector=vector)
            for chunk, vector in zip(outstanding, vectors)
        ]

        return self.db.embeddings.store(
            pending, task=self.task, model=self.model, commit=commit
        )
