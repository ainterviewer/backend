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
        return self.settings.enabled and bool(self.settings.base_url)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            if not self.settings.base_url:
                raise EmbeddingUnavailable("No embedding endpoint configured")
            self._client = httpx.AsyncClient(
                base_url=self.settings.base_url,
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
        """Whether embeddings can be served right now. Never raises or hangs.

        Asks the load balancer's pool-scoped probe rather than its own
        `/health`: the proxy answers that one as long as the proxy itself is
        up, which says nothing about whether any embedding instance is running
        behind it.

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
                "/v1/embeddings/health", timeout=self.settings.connect_timeout
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
                    "/v1/embeddings",
                    json={
                        "input": inputs,
                        "model": self.settings.model,
                        "encoding_format": "float",
                    },
                )
                # A 503 carrying `Retry-After` is the load balancer saying an
                # instance is on its way up. That is a wait, not a failure:
                # counting it would let a routine scale-up open the circuit
                # breaker and degrade search for the cooldown on top of the
                # boot. Back off for as long as we are told, within reason.
                if response.status_code == 503 and "retry-after" in response.headers:
                    last_error = EmbeddingUnavailable(
                        f"Embedding capacity starting: {response.text[:200]}"
                    )
                    if attempt < self.settings.max_retries - 1:
                        await asyncio.sleep(
                            min(
                                self._retry_after(response.headers["retry-after"]),
                                self.settings.max_retry_after,
                            )
                        )
                    continue

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

                vectors = [item["embedding"] for item in response.json()["data"]]
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

    @staticmethod
    def _retry_after(header: str) -> float:
        """`Retry-After` in seconds, falling back to a short wait.

        Only the delta-seconds form is handled: the load balancer sends that,
        and an unparsable value should not turn a retryable 503 into a crash.
        """
        try:
            return max(float(header), 0.0)
        except ValueError:
            return 5.0

    def _batches(self, inputs: list[str]) -> list[list[int]]:
        """Group `inputs` into requests, returning indices into `inputs`.

        `batch_size` alone is the server's rejection threshold, not a statement
        about how long a request takes: the chunks vary from a single message to
        a whole transcript, so the same 32 inputs can be a few thousand
        characters or the better part of a million. Batched by count only, the
        long ones queue up behind each other inside one request and run past
        `timeout`, which surfaces as an unreachable server and drops the whole
        batch to the backfill.

        The size budget is on `count x longest`, not on the sum: the server pads
        every input in a batch out to the longest one, so one interview-level
        chunk sets the price for everything sharing its request -- measured, one
        long chunk plus seven short ones costs the same as eight long ones. That
        is also why these are grouped by length: the queue interleaves message-,
        section- and interview-level chunks, and taking them in arrival order
        puts the expensive ones next to cheap ones that then pay their price.

        Characters rather than tokens, to stay free of a tokenizer; at ~3.4
        chars/token that errs towards smaller requests. An input over the budget
        on its own still goes -- `truncate` has already capped it.
        """
        batches: list[list[int]] = []
        current: list[int] = []

        # Ascending, so the input being considered is always the longest in the
        # batch it would join and its length alone sets the padded cost.
        for index in sorted(range(len(inputs)), key=lambda i: len(inputs[i])):
            padded = (len(current) + 1) * len(inputs[index])
            if current and (
                len(current) == self.settings.batch_size
                or padded > self.settings.max_batch_chars
            ):
                batches.append(current)
                current = []
            current.append(index)

        if current:
            batches.append(current)

        return batches

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed passages, document-side: no instruction prefix, by design.

        Returns one vector per input, in order.
        """
        if not texts:
            return []

        inputs = [self.truncate(text) for text in texts]
        # Placed by index: `_batches` groups by length, so requests come back
        # in no relation to the order the caller asked in.
        vectors: list[list[float] | None] = [None] * len(inputs)

        for batch in self._batches(inputs):
            returned = await self._post_embed([inputs[i] for i in batch])
            if len(returned) != len(batch):
                raise EmbeddingUnavailable(
                    f"Embedding server returned {len(returned)} vectors for "
                    f"{len(batch)} inputs"
                )
            for index, vector in zip(batch, returned):
                vectors[index] = vector

        if any(vector is None for vector in vectors):
            raise EmbeddingUnavailable(
                "Embedding server did not return a vector for every input"
            )

        return [vector for vector in vectors if vector is not None]

    async def embed_query(
        self, query: str, task: QueryTask = QueryTask.RETRIEVAL
    ) -> list[float]:
        """Embed a search query under its task instruction."""
        vectors = await self._post_embed([self.truncate(build_query(query, task))])
        return vectors[0]


embedding_client = EmbeddingClient()
