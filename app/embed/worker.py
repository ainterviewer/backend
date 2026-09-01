"""Background worker that drains the chunk queue into stored vectors."""

import asyncio
import logging

from sqlalchemy.orm import Session

from ..db import InterviewDataBase
from ..dependencies import engine
from ..settings import app_settings
from .client import EmbeddingClient, EmbeddingUnavailable, embedding_client
from .queue import ChunkQueue, chunk_queue
from .service import EmbeddingService

logger = logging.getLogger(__name__)


class EmbeddingWorker:
    """Batches queued chunks and embeds them.

    Runs one batch at a time against the shared client, so interviews never
    compete with each other for the inference server, and opens a fresh session
    per batch rather than holding one for the process lifetime.

    A failed batch is dropped rather than requeued: retrying inside the worker
    would let a permanently bad batch block the queue, and the backfill already
    re-derives anything missing. What matters here is that the loop survives.
    """

    def __init__(
        self,
        queue: ChunkQueue | None = None,
        client: EmbeddingClient | None = None,
        batch_size: int | None = None,
        batch_timeout: float = 2.0,
    ):
        self.queue = queue or chunk_queue
        self.client = client or embedding_client
        self.batch_size = batch_size or app_settings.services.embedding.batch_size
        self.batch_timeout = batch_timeout
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if not self.client.enabled:
            logger.info("Embedding disabled; not starting the embedding worker")
            return
        if self.running:
            return

        self._task = asyncio.create_task(self._run(), name="embedding-worker")
        logger.info("Embedding worker started")

    async def stop(self) -> None:
        if self._task is None:
            return

        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

        await self.client.aclose()
        logger.info("Embedding worker stopped")

    async def _run(self) -> None:
        while True:
            try:
                batch = await self.queue.collect(self.batch_size, self.batch_timeout)
                await self._process(batch)
            except asyncio.CancelledError:
                raise
            except Exception:
                # The loop is the thing that must not die. Anything that gets
                # here is already lost to the backfill, not to the respondent.
                logger.exception("Embedding worker batch failed")
                await asyncio.sleep(1)

    async def _process(self, batch: list) -> None:
        if not batch:
            return

        with Session(engine) as session:
            db = InterviewDataBase(session)
            service = EmbeddingService(db, self.client)

            try:
                written = await service.embed_and_store(batch)
            except EmbeddingUnavailable as error:
                logger.warning(
                    "Dropped %s queued chunk(s): %s. The backfill will pick them up.",
                    len(batch),
                    error,
                )
                return

        logger.debug("Embedded %s of %s queued chunk(s)", written, len(batch))


embedding_worker = EmbeddingWorker()
