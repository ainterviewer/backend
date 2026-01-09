from fastapi import APIRouter

router = APIRouter()


@router.get("/messages/search")
async def semantic_search(query: str, limit: int = 10):
    """
    Semantic search endpoint

    Flow:
    1. Generate embedding for query
    2. Search vector store
    3. Return similar messages
    """
    # Generate query embedding
    query_embedding = await embedding_service.generate_embedding(query)

    # Search for similar messages
    results = await vector_store.search_similar(query_embedding, limit=limit)

    return {"results": results}


@router.get("/emebeddings/queue/status")
async def queue_status():
    """Monitor queue health for scaling decisions"""
    depth = await message_queue.get_queue_depth()
    return {"queue_depth": depth, "status": "healthy" if depth < 1000 else "degraded"}
