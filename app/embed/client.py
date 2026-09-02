"""HTTP client for a text-embeddings-inference (TEI) server.

Vectors come back L2-normalised (`normalize: true`), so cosine similarity is a
plain dot product everywhere downstream and nothing needs to normalise again.
"""

import asyncio
import logging
import time

import httpx

from ..settings import EmbeddingSettings, app_settings
from .templates import QueryTask, build_query

logger = logging.getLogger(__name__)


class EmbeddingUnavailable(RuntimeError):
    """The embedding server could not be reached, or refused the request.

    Callers on a user-facing path should catch this and degrade -- to text
    search, or to "not embedded yet" -- rather than surfacing a 500.
    """


class EmbeddingClient:
    """Batching, retrying client for one TEI server.

    Holds a circuit breaker: after `circuit_breaker_threshold` consecutive
    failures it fails fast for `circuit_breaker_reset_seconds` instead of making
    every caller wait out the timeout and retries. A single success closes it.
    """

    def __init__(self, settings: EmbeddingSettings | None = None):
        self.settings = settings or app_settings.services.embedding
        self._client: httpx.AsyncClient | None = None
        self._failures = 0
        self._open_until = 0.0

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    @property
    def enabled(self) -> bool:
        return self.settings.enabled and bool(self.settings.endpoint)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            if not self.settings.endpoint:
                raise EmbeddingUnavailable("No embedding endpoint configured")
            self._client = httpx.AsyncClient(
                base_url=self.settings.endpoint,
                timeout=httpx.Timeout(
                    self.settings.timeout,
                    connect=self.settings.connect_timeout,
                ),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def health(self) -> bool:
        """Whether the server is reachable. Never raises, and never hangs.

        Answered from the circuit breaker while it is open: the last few calls
        already established the server is down, and a status read is not the
        place to check again at the cost of a timeout. Otherwise probed under
        `connect_timeout` rather than the inference timeout -- callers put this
        in front of a page, so an unreachable box has to come back as `False`
        in seconds, not minutes.
        """
        if not self.enabled:
            return False
        if self.circuit_open:
            return False
        try:
            response = await self._http().get(
                "/health", timeout=self.settings.connect_timeout
            )
        except Exception:
            return False
        return response.status_code == 200

    # ------------------------------------------------------------------ #
    # Circuit breaker                                                    #
    # ------------------------------------------------------------------ #

    @property
    def circuit_open(self) -> bool:
        """True while the client is failing fast after repeated failures."""
        return bool(self._open_until) and time.monotonic() < self._open_until

    def _check_circuit(self) -> None:
        if self._open_until and time.monotonic() < self._open_until:
            raise EmbeddingUnavailable(
                "Embedding server marked unavailable after "
                f"{self._failures} consecutive failures"
            )

    def _record_success(self) -> None:
        self._failures = 0
        self._open_until = 0.0

    def _record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.settings.circuit_breaker_threshold:
            self._open_until = (
                time.monotonic() + self.settings.circuit_breaker_reset_seconds
            )
            logger.error(
                "Embedding server unreachable after %s consecutive failures; "
                "failing fast for %ss",
                self._failures,
                self.settings.circuit_breaker_reset_seconds,
            )

    # ------------------------------------------------------------------ #
    # Embedding                                                          #
    # ------------------------------------------------------------------ #

    def truncate(self, text: str) -> str:
        """Cut `text` to the configured character budget.

        The server truncates silently when `--auto-truncate` is on. Doing it
        here instead keeps the stored `content_hash` a hash of the text that was
        actually embedded, and makes the loss visible in the logs.
        """
        limit = self.settings.max_input_chars
        if len(text) <= limit:
            return text
        logger.info("Truncating embedding input from %s to %s chars", len(text), limit)
        return text[:limit]

    async def _post_embed(self, inputs: list[str]) -> list[list[float]]:
        self._check_circuit()

        delay = 1.0
        last_error: Exception | None = None

        for attempt in range(self.settings.max_retries):
            try:
                response = await self._http().post(
                    "/embed",
                    json={"inputs": inputs, "normalize": True, "truncate": True},
                )
                # 4xx other than rate limiting is our bug (bad batch size,
                # malformed payload) and will not fix itself on a retry.
                if response.status_code == 429 or response.status_code >= 500:
                    response.raise_for_status()
                if response.status_code >= 400:
                    self._record_failure()
                    raise EmbeddingUnavailable(
                        f"Embedding server rejected the request "
                        f"({response.status_code}): {response.text[:200]}"
                    )

                vectors = response.json()
                self._record_success()
                return vectors

            except EmbeddingUnavailable:
                raise
            except Exception as error:  # network errors and 5xx/429
                last_error = error
                if attempt < self.settings.max_retries - 1:
                    await asyncio.sleep(delay)
                    delay *= 2

        self._record_failure()
        raise EmbeddingUnavailable(
            f"Embedding server unreachable after {self.settings.max_retries} "
            f"attempts: {last_error}"
        ) from last_error

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed passages, document-side: no instruction prefix, by design.

        Returns one vector per input, in order.
        """
        if not texts:
            return []

        inputs = [self.truncate(text) for text in texts]
        vectors: list[list[float]] = []

        for start in range(0, len(inputs), self.settings.batch_size):
            batch = inputs[start : start + self.settings.batch_size]
            vectors.extend(await self._post_embed(batch))

        if len(vectors) != len(texts):
            raise EmbeddingUnavailable(
                f"Embedding server returned {len(vectors)} vectors for "
                f"{len(texts)} inputs"
            )

        return vectors

    async def embed_query(
        self, query: str, task: QueryTask = QueryTask.RETRIEVAL
    ) -> list[float]:
        """Embed a search query under its task instruction."""
        vectors = await self._post_embed([self.truncate(build_query(query, task))])
        return vectors[0]


embedding_client = EmbeddingClient()
