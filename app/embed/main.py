"""
Chat Message Embedding Architecture - FastAPI WebSocket Implementation

Key Performance Considerations:
1. Decouple message delivery from embedding generation
2. Use async workers for embedding generation
3. Batch embeddings when possible
4. Consider rate limiting for embedding API calls
5. Implement proper error handling and retries
6. Use connection pooling for DB operations
7. Monitor queue depth and processing latency

Architecture Components:
- WebSocket handler: Real-time message delivery
- Message queue: Async task distribution
- Embedding worker: Background processing
- Vector store: Persistence layer
"""

import asyncio
import random
import time
from typing import Dict, List

from ainterviewer.utils import now


class EmbeddingTask:
    """Task for embedding generation"""

    def __init__(self, message_id: int, content: str, priority: int = 0):
        self.message_id = message_id
        self.content = content
        self.priority = priority
        self.retry_count = 0
        self.created_at = now()

    def __lt__(self, other):
        # Higher priority (lower number) first
        if self.priority != other.priority:
            return self.priority < other.priority
        # FIFO for same priority
        return self.created_at < other.created_at


# ============================================================================
# Core Services
# ============================================================================


class MessageQueue:
    """
    Async message queue for embedding tasks

    Consider using:
    - Redis with aioredis for distributed systems
    - RabbitMQ with aio-pika for complex routing
    - In-memory asyncio.Queue for single-server setups
    """

    def __init__(self, maxsize: int = 10000):
        # Using PriorityQueue to handle task priorities
        self.queue = asyncio.PriorityQueue(maxsize=maxsize)
        self.maxsize = maxsize

    async def enqueue(self, task: EmbeddingTask):
        """Add embedding task to queue"""
        await self.queue.put(task)

    async def dequeue(self) -> EmbeddingTask:
        """Get next task from queue"""
        return await self.queue.get()

    async def get_queue_depth(self) -> int:
        """Monitor queue size for scaling decisions"""
        return self.queue.qsize()


class EmbeddingService:
    """
    Handles embedding generation

    Performance tips:
    - Batch multiple messages together when possible
    - Implement exponential backoff for retries
    - Use connection pooling for embedding API
    - Cache embeddings for identical content
    - Consider multiple workers for parallel processing
    """

    def __init__(self, batch_size: int = 32, batch_timeout: float = 1.0):
        self.batch_size = batch_size
        self.batch_timeout = batch_timeout
        self.pending_batch = []
        # Simulate model dimension
        self.embedding_dim = 1536

    async def generate_embedding(self, text: str) -> List[float]:
        """
        Generate embedding for single text

        Replace with actual implementation:
        - OpenAI embeddings
        - Sentence Transformers (local)
        - Cohere embeddings
        - Custom model
        """
        # Simulate network latency
        await asyncio.sleep(0.05)
        # Return random vector
        return [random.random() for _ in range(self.embedding_dim)]

    async def generate_embeddings_batch(self, texts: List[str]) -> List[List[float]]:
        """Generate embeddings for multiple texts efficiently"""
        # Simulate network latency (batch processing is usually faster per item)
        await asyncio.sleep(0.1)
        return [
            [random.random() for _ in range(self.embedding_dim)]
            for _ in range(len(texts))
        ]

    async def process_with_retry(self, task: EmbeddingTask, max_retries: int = 3):
        """Process task with exponential backoff retry logic"""
        delay = 1.0
        for attempt in range(max_retries):
            try:
                return await self.generate_embedding(task.content)
            except Exception as e:
                task.retry_count += 1
                if attempt == max_retries - 1:
                    print(
                        f"Failed to generate embedding for task {task.message_id}: {e}"
                    )
                    raise

                print(f"Retry {attempt + 1}/{max_retries} for task {task.message_id}")
                await asyncio.sleep(delay)
                delay *= 2  # Exponential backoff
        return None


class VectorStore:
    """
    Manages vector storage and retrieval

    Options:
    - PostgreSQL with pgvector extension
    - Pinecone, Weaviate, Qdrant (managed vector DBs)
    - Elasticsearch with dense_vector
    - ChromaDB (local development)
    """

    def __init__(self):
        # In-memory storage for prototype
        self._store: Dict[str, List[float]] = {}
        self._status: Dict[str, str] = {}

    async def save_embedding(self, message_id: int, embedding: List[float]):
        """Persist embedding to vector store"""
        # Simulate DB IO
        await asyncio.sleep(0.01)
        self._store[str(message_id)] = embedding
        await self.update_embedding_status(message_id, "completed")
        print(f"Saved embedding for message {message_id}")

    async def search_similar(self, query_embedding: List[float], limit: int = 10):
        """Semantic search for similar messages"""
        # Simulate search latency
        await asyncio.sleep(0.05)
        # Dummy result - in reality would use cosine similarity
        return list(self._store.keys())[:limit]

    async def update_embedding_status(self, message_id: int, status: str):
        """Update processing status in database"""
        self._status[str(message_id)] = status


# ============================================================================
# Background Workers
# ============================================================================


class EmbeddingWorker:
    """
    Background worker for processing embedding queue

    Scaling considerations:
    - Run multiple workers for parallel processing
    - Deploy workers separately from API servers
    - Use distributed queue for multi-server setup
    - Monitor processing rate and adjust worker count
    """

    def __init__(
        self,
        queue: MessageQueue,
        embedding_service: EmbeddingService,
        vector_store: VectorStore,
    ):
        self.queue = queue
        self.embedding_service = embedding_service
        self.vector_store = vector_store
        self.is_running = False

    async def start(self):
        """Start processing queue"""
        self.is_running = True
        # Run process loop in background
        asyncio.create_task(self._process_loop())

    async def stop(self):
        """Gracefully stop worker"""
        self.is_running = False

    async def _process_loop(self):
        """Main processing loop"""
        print("Starting embedding worker loop")
        while self.is_running:
            try:
                task = await self.queue.dequeue()
                await self._process_task(task)
                # Mark task as done in the queue
                # self.queue.queue.task_done() # if using join()
            except Exception as e:
                print(f"Error in worker loop: {e}")
                await asyncio.sleep(1)

    async def _process_task(self, task: EmbeddingTask):
        """Process single embedding task"""
        try:
            await self.vector_store.update_embedding_status(
                task.message_id, "processing"
            )
            embedding = await self.embedding_service.process_with_retry(task)
            if embedding:
                await self.vector_store.save_embedding(task.message_id, embedding)
        except Exception as e:
            print(f"Failed to process task {task.message_id}: {e}")
            await self.vector_store.update_embedding_status(task.message_id, "failed")


class BatchEmbeddingWorker:
    """
    Optimized worker that batches multiple tasks together

    Benefits:
    - Reduces API calls to embedding service
    - Better throughput for high-volume scenarios
    - More efficient GPU utilization for local models
    """

    def __init__(
        self,
        queue: MessageQueue,
        embedding_service: EmbeddingService,
        vector_store: VectorStore,
        batch_size: int = 32,
        batch_timeout: float = 1.0,
    ):
        self.queue = queue
        self.embedding_service = embedding_service
        self.vector_store = vector_store
        self.batch_size = batch_size
        self.batch_timeout = batch_timeout
        self.is_running = False

    async def start(self):
        """Start processing queue"""
        self.is_running = True
        asyncio.create_task(self._process_loop())

    async def stop(self):
        self.is_running = False

    async def _process_loop(self):
        print("Starting batch embedding worker loop")
        while self.is_running:
            try:
                batch = await self._collect_batch()
                if batch:
                    await self._process_batch(batch)
                else:
                    # Avoid tight loop if queue is empty
                    await asyncio.sleep(0.1)
            except Exception as e:
                print(f"Error in batch worker loop: {e}")
                await asyncio.sleep(1)

    async def _collect_batch(self) -> List[EmbeddingTask]:
        """Collect tasks up to batch_size or timeout"""
        batch = []
        start_time = time.time()

        while len(batch) < self.batch_size:
            # Calculate remaining time
            elapsed = time.time() - start_time
            remaining = self.batch_timeout - elapsed

            if remaining <= 0 and batch:
                # Timeout reached and we have items
                break

            try:
                # Wait for next item with timeout
                # If batch is empty, we can wait longer (or indefinitely if we prefer)
                # but here we use timeout to check is_running
                wait_time = remaining if batch else 1.0
                if wait_time < 0:
                    wait_time = 0

                # We need to wrap dequeue in wait_for because dequeue waits indefinitely
                task = await asyncio.wait_for(self.queue.dequeue(), timeout=wait_time)
                batch.append(task)
            except asyncio.TimeoutError:
                # Timeout reached
                if batch:
                    break
                # If batch is empty and we timed out, loop again to check is_running/timeout
                if elapsed > self.batch_timeout:
                    # Reset start time if we were just waiting for the first item
                    # to avoid immediate timeout loop
                    pass
            except Exception:
                break

        return batch

    async def _process_batch(self, tasks: List[EmbeddingTask]):
        """Process batch of tasks together"""
        if not tasks:
            return

        print(f"Processing batch of {len(tasks)} tasks")
        try:
            # Update status for all
            for task in tasks:
                await self.vector_store.update_embedding_status(
                    task.message_id, "processing"
                )

            # Generate embeddings
            texts = [task.content for task in tasks]
            embeddings = await self.embedding_service.generate_embeddings_batch(texts)

            # Save results
            for task, embedding in zip(tasks, embeddings):
                await self.vector_store.save_embedding(task.message_id, embedding)

        except Exception as e:
            print(f"Batch processing failed: {e}")
            for task in tasks:
                await self.vector_store.update_embedding_status(
                    task.message_id, "failed"
                )


# Initialize services (in real app, use dependency injection)
message_queue = MessageQueue()
embedding_service = EmbeddingService()
vector_store = VectorStore()
embedding_worker = EmbeddingWorker(
    message_queue,
    embedding_service,
    vector_store,
)

# ============================================================================
# Additional Performance Considerations
# ============================================================================

"""
1. CACHING STRATEGY:
   - Cache embeddings for frequently accessed messages
   - Use Redis for distributed cache
   - Implement cache warming for popular content

2. RATE LIMITING:
   - Limit embedding API calls to avoid quotas
   - Implement token bucket for smooth rate limiting
   - Consider user-based or session-based limits

3. MONITORING:
   - Track queue depth over time
   - Monitor embedding generation latency
   - Alert on high queue depth or processing failures
   - Track WebSocket connection count

4. SCALING:
   - Horizontal scaling: Run multiple workers
   - Vertical scaling: Increase worker resources
   - Use auto-scaling based on queue depth
   - Consider separate embedding service deployment

5. ERROR HANDLING:
   - Dead letter queue for failed tasks
   - Exponential backoff for retries
   - Circuit breaker for embedding service
   - Graceful degradation when embeddings fail

6. DATABASE OPTIMIZATION:
   - Index frequently queried fields
   - Use connection pooling
   - Partition large message tables by date
   - Consider read replicas for search queries

7. BATCHING STRATEGIES:
   - Batch by size (e.g., 32 messages)
   - Batch by time (e.g., every 1 second)
   - Adaptive batching based on queue depth
   - Priority-based batching for important messages

8. COST OPTIMIZATION:
   - Use local embedding models when possible
   - Deduplicate identical messages before embedding
   - Compress embeddings for storage
   - Archive old embeddings to cold storage
"""
