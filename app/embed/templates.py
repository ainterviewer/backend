"""Query-side instruction templates for the embedding model.

harrier-oss is asymmetric, and the asymmetry runs one way only: its model card
specifies `"Instruct: {task_description}\\nQuery: {query}"` for queries and
states that passages "are encoded directly without prefixes". So a document is
embedded once, task-free, and the downstream task is expressed entirely in how
the *query* is templated at search time. This is why there is one stored vector
per chunk rather than one per task.

Instructions are mandatory on the query side -- the card warns that omitting
them degrades performance -- so `build_query` never emits a bare query.

`TEMPLATE_VERSION` is recorded alongside stored evaluation results: changing an
instruction string changes the query geometry, and a comparison across a change
is not a comparison of the same thing. It does *not* invalidate stored document
vectors, which is the whole point of keeping the tasks on this side.
"""

from enum import StrEnum

TEMPLATE_VERSION = "1"

QUERY_TEMPLATE = "Instruct: {task_description}\nQuery: {query}"


class QueryTask(StrEnum):
    RETRIEVAL = "retrieval"
    SIMILARITY = "similarity"
    CLASSIFICATION = "classification"
    CLUSTERING = "clustering"


TASK_DESCRIPTIONS: dict[QueryTask, str] = {
    QueryTask.RETRIEVAL: (
        "Given a search query, retrieve passages from qualitative research "
        "interviews that answer the query"
    ),
    QueryTask.SIMILARITY: (
        "Retrieve interview passages that are semantically similar to the given passage"
    ),
    QueryTask.CLASSIFICATION: (
        "Given a description of a theme, retrieve interview passages in which a "
        "respondent expresses that theme"
    ),
    QueryTask.CLUSTERING: (
        "Identify the topic or theme expressed in the given interview passage"
    ),
}


def build_query(query: str, task: QueryTask = QueryTask.RETRIEVAL) -> str:
    """Render a query under its task instruction."""
    return QUERY_TEMPLATE.format(
        task_description=TASK_DESCRIPTIONS[task],
        query=query.strip(),
    )
