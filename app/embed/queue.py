"""The hand-off between a running interview and the embedding worker.

Deliberately lossy. `QueueEmbedder.embed_chunk` sits on the interview's
critical path, so it never blocks and never raises: if the queue is full --
the embedding server is down, or slower than interviews are producing text --
the chunk is dropped and counted. Nothing is lost permanently, because
`app/embed/backfill.py` re-derives every chunk from the stored messages; a drop
costs freshness, not data. Blocking here would cost a respondent their session.
"""

import asyncio
import logging

from ainterviewer.interfaces import EmbeddingChunk

logger = logging.getLogger(__name__)

DEFAULT_MAXSIZE = 10_000


class ChunkQueue:
    """An in-memory queue of chunks awaiting embedding.

    In-memory on purpose: this is a cache-warming path, not a durable one. A
    restart drops whatever is queued, which is exactly the case the backfill
    already covers.
    """

    def __init__(self, maxsize: int = DEFAULT_MAXSIZE):
        self._queue: asyncio.Queue[EmbeddingChunk] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0

    @property
    def depth(self) -> int:
        return self._queue.qsize()

    def put(self, chunk: EmbeddingChunk) -> bool:
        """Enqueue without waiting. False if the queue was full."""
        try:
            self._queue.put_nowait(chunk)
            return True
        except asyncio.QueueFull:
            self.dropped += 1
            logger.warning(
                "Embedding queue full (%s); dropped a %s chunk for interview %s. "
                "The backfill will pick it up.",
                self._queue.maxsize,
                chunk.kind.value,
                chunk.interview_id,
            )
            return False

    async def get(self) -> EmbeddingChunk:
        return await self._queue.get()

    async def collect(self, max_items: int, timeout: float) -> list[EmbeddingChunk]:
        """Wait for one chunk, then take up to `max_items` more that arrive
        within `timeout`. Returns an empty list only if cancelled."""
        batch = [await self.get()]

        while len(batch) < max_items:
            try:
                batch.append(await asyncio.wait_for(self.get(), timeout=timeout))
            except TimeoutError:
                break

        return batch


class QueueEmbedder:
    """`EmbeddingProtocol` implementation backed by a `ChunkQueue`."""

    def __init__(self, queue: ChunkQueue):
        self.queue = queue

    async def embed_chunk(self, chunk: EmbeddingChunk) -> None:
        self.queue.put(chunk)


chunk_queue = ChunkQueue()
