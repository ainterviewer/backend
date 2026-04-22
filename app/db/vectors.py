"""
SQLAlchemy interface for the sqlite-vector extension.

This module provides a SQLAlchemy-compatible class to interface with the sqlite-vector
extension for efficient vector operations in SQLite databases.

Usage:
    from sqlalchemy.orm import Session
    from app.db.vectors import VectorExtension, VectorType, DistanceFunction
    from app.db.tables import DocumentTable

    # Initialize the extension for a table
    with Session(engine) as session:
        vectors = VectorExtension(session)
        vectors.init(DocumentTable, DocumentTable.embedding, dimension=384, distance=DistanceFunction.COSINE)

        # Insert vectors
        doc = DocumentTable(content="hello", embedding=vectors.encode([0.1, 0.2, 0.3]))
        session.add(doc)

        # Perform similarity search
        results = vectors.search(
            DocumentTable, DocumentTable.embedding,
            query_vector=[0.1, 0.2, 0.3],
            k=10,
            use_quantization=True
        )
"""

from __future__ import annotations

import importlib.resources
import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import (
    TYPE_CHECKING,
    Any,
    Iterator,
    Literal,
    Sequence,
    TypeVar,
    overload,
)

from sqlalchemy import Engine, Row, event, text
from sqlalchemy.orm import DeclarativeBase, InstrumentedAttribute, Session

if TYPE_CHECKING:
    pass


# Type variable for table classes
T = TypeVar("T", bound=DeclarativeBase)

# Type alias for table/column specification
TableRef = type[DeclarativeBase] | str
ColumnRef = InstrumentedAttribute[Any] | str


class VectorType(StrEnum):
    """Supported vector data types for the sqlite-vector extension."""

    FLOAT32 = "FLOAT32"
    FLOAT16 = "FLOAT16"
    FLOATB16 = "FLOATB16"  # bfloat16
    INT8 = "INT8"
    UINT8 = "UINT8"


class DistanceFunction(StrEnum):
    """Distance functions available for vector similarity search."""

    L2 = "L2"
    SQUARED_L2 = "SQUARED_L2"
    COSINE = "COSINE"
    DOT = "DOT"
    L1 = "L1"


# Mapping from VectorType to SQL encoding function names
_ENCODE_FUNC_MAP: dict[VectorType, str] = {
    VectorType.FLOAT32: "vector_as_f32",
    VectorType.FLOAT16: "vector_as_f16",
    VectorType.FLOATB16: "vector_as_bf16",
    VectorType.INT8: "vector_as_i8",
    VectorType.UINT8: "vector_as_u8",
}


@dataclass
class VectorSearchResult:
    """Result from a vector similarity search."""

    rowid: int
    distance: float


@dataclass
class VectorConfig:
    """Configuration for a vector column."""

    table: str
    column: str
    dimension: int
    vector_type: VectorType = VectorType.FLOAT32
    distance: DistanceFunction = DistanceFunction.L2


def _resolve_table_name(table: TableRef) -> str:
    """Extract table name from a DeclarativeBase class or string."""
    if isinstance(table, str):
        return table
    if hasattr(table, "__tablename__"):
        return table.__tablename__
    raise ValueError(f"Cannot determine table name from {table}")


def _resolve_column_name(column: ColumnRef) -> str:
    """Extract column name from an InstrumentedAttribute or string."""
    if isinstance(column, str):
        return column
    if hasattr(column, "key"):
        return column.key
    raise ValueError(f"Cannot determine column name from {column}")


def _resolve_table_and_column(table: TableRef, column: ColumnRef) -> tuple[str, str]:
    """Resolve both table and column names."""
    return _resolve_table_name(table), _resolve_column_name(column)


class VectorExtension:
    """
    SQLAlchemy-compatible interface for the sqlite-vector extension.

    This class provides methods to initialize vector columns, encode vectors,
    perform quantization, and execute similarity searches.

    Supports both string-based and SQLAlchemy DeclarativeBase table/column references.

    Attributes:
        session: SQLAlchemy session for database operations.

    Example:
        >>> from app.db.tables import DocumentTable
        >>> vectors = VectorExtension(session)
        >>> vectors.init(DocumentTable, DocumentTable.embedding, dimension=384)
        >>> vectors.quantize(DocumentTable, DocumentTable.embedding)
        >>> results = vectors.search(DocumentTable, DocumentTable.embedding, [0.1, 0.2, ...], k=10)
    """

    def __init__(self, session: Session):
        """
        Initialize the VectorExtension.

        Args:
            session: SQLAlchemy session for database operations.
        """
        self.session = session
        self._initialized_configs: dict[tuple[str, str], VectorConfig] = {}

    # ==================== Extension Info ====================

    def version(self) -> str:
        """
        Get the version of the sqlite-vector extension.

        Returns:
            Version string (e.g., '1.0.0').
        """
        result = self.session.execute(text("SELECT vector_version()"))
        return result.scalar_one()

    def backend(self) -> str:
        """
        Get the active backend used for vector computation.

        Returns:
            Backend name: 'CPU', 'SSE2', 'AVX2', or 'NEON'.
        """
        result = self.session.execute(text("SELECT vector_backend()"))
        return result.scalar_one()

    # ==================== Initialization ====================

    def init(
        self,
        table: TableRef,
        column: ColumnRef,
        dimension: int,
        vector_type: VectorType = VectorType.FLOAT32,
        distance: DistanceFunction = DistanceFunction.L2,
    ) -> None:
        """
        Initialize a table column for vector operations.

        This must be called before performing any vector search or quantization.
        Must be called in every database connection that needs vector operations.

        Args:
            table: Table class (DeclarativeBase subclass) or table name string.
            column: Column attribute (e.g., MyTable.embedding) or column name string.
            dimension: Length of each vector (required).
            vector_type: Vector data type (default: FLOAT32).
            distance: Distance function to use (default: L2).

        Note:
            The target table must have a rowid (integer primary key).
            If created with WITHOUT ROWID, it must have exactly one INTEGER primary key.

        Example:
            >>> vectors.init(DocumentTable, DocumentTable.embedding, dimension=384)
            >>> # Or with strings:
            >>> vectors.init("documents", "embedding", dimension=384)
        """
        table_name, column_name = _resolve_table_and_column(table, column)

        options = (
            f"dimension={dimension},type={vector_type.value},distance={distance.value}"
        )
        self.session.execute(
            text("SELECT vector_init(:table, :column, :options)"),
            {"table": table_name, "column": column_name, "options": options},
        )

        # Store config for later reference
        config = VectorConfig(
            table=table_name,
            column=column_name,
            dimension=dimension,
            vector_type=vector_type,
            distance=distance,
        )
        self._initialized_configs[(table_name, column_name)] = config

    def get_config(self, table: TableRef, column: ColumnRef) -> VectorConfig | None:
        """
        Get the configuration for an initialized vector column.

        Args:
            table: Table class or name.
            column: Column attribute or name.

        Returns:
            VectorConfig if the column has been initialized, None otherwise.
        """
        table_name, column_name = _resolve_table_and_column(table, column)
        return self._initialized_configs.get((table_name, column_name))

    # ==================== Vector Encoding ====================

    @staticmethod
    def _vector_to_json(vector: Sequence[float | int]) -> str:
        """Convert a vector to JSON string format."""
        return json.dumps(list(vector))

    def encode(
        self,
        vector: Sequence[float | int],
        vector_type: VectorType = VectorType.FLOAT32,
        dimension: int | None = None,
    ) -> bytes:
        """
        Encode a vector into the required internal BLOB format.

        This should be used when setting vector column values in INSERT/UPDATE operations.

        Args:
            vector: Vector as a sequence of numbers.
            vector_type: Target vector type (default: FLOAT32).
            dimension: Optional dimension for stricter validation.

        Returns:
            Encoded vector as bytes (BLOB).

        Example:
            >>> encoded = vectors.encode([0.1, 0.2, 0.3])
            >>> doc = DocumentTable(content="hello", embedding=encoded)
            >>> session.add(doc)
        """
        func_name = _ENCODE_FUNC_MAP[vector_type]
        json_vec = self._vector_to_json(vector)

        if dimension is not None:
            result = self.session.execute(
                text(f"SELECT {func_name}(:vec, :dim)"),
                {"vec": json_vec, "dim": dimension},
            )
        else:
            result = self.session.execute(
                text(f"SELECT {func_name}(:vec)"),
                {"vec": json_vec},
            )
        return result.scalar_one()

    def encode_f32(
        self, vector: Sequence[float], dimension: int | None = None
    ) -> bytes:
        """Encode a vector as FLOAT32."""
        return self.encode(vector, VectorType.FLOAT32, dimension)

    def encode_f16(
        self, vector: Sequence[float], dimension: int | None = None
    ) -> bytes:
        """Encode a vector as FLOAT16."""
        return self.encode(vector, VectorType.FLOAT16, dimension)

    def encode_bf16(
        self, vector: Sequence[float], dimension: int | None = None
    ) -> bytes:
        """Encode a vector as BFLOAT16."""
        return self.encode(vector, VectorType.FLOATB16, dimension)

    def encode_i8(self, vector: Sequence[int], dimension: int | None = None) -> bytes:
        """Encode a vector as INT8."""
        return self.encode(vector, VectorType.INT8, dimension)

    def encode_u8(self, vector: Sequence[int], dimension: int | None = None) -> bytes:
        """Encode a vector as UINT8."""
        return self.encode(vector, VectorType.UINT8, dimension)

    # ==================== Quantization ====================

    def quantize(
        self,
        table: TableRef,
        column: ColumnRef,
        max_memory: str | None = None,
    ) -> int:
        """
        Perform quantization on a vector column for fast approximate nearest neighbor search.

        This precomputes internal data structures for ANN search. Should be called once
        after data insertion. If called multiple times, previous quantized data is replaced.

        Args:
            table: Table class or name.
            column: Column attribute or name.
            max_memory: Maximum memory for quantization (e.g., '50MB'). Default: 30MB.

        Returns:
            Total number of successfully quantized rows.

        Note:
            The resulting quantization is shared across all database connections.

        Example:
            >>> vectors.quantize(DocumentTable, DocumentTable.embedding, max_memory="50MB")
        """
        table_name, column_name = _resolve_table_and_column(table, column)

        if max_memory:
            options = f"max_memory={max_memory}"
            result = self.session.execute(
                text("SELECT vector_quantize(:table, :column, :options)"),
                {"table": table_name, "column": column_name, "options": options},
            )
        else:
            result = self.session.execute(
                text("SELECT vector_quantize(:table, :column)"),
                {"table": table_name, "column": column_name},
            )
        return result.scalar_one()

    def quantize_memory(self, table: TableRef, column: ColumnRef) -> int:
        """
        Get the memory required to preload quantized data.

        Args:
            table: Table class or name.
            column: Column attribute or name.

        Returns:
            Memory required in bytes.
        """
        table_name, column_name = _resolve_table_and_column(table, column)
        result = self.session.execute(
            text("SELECT vector_quantize_memory(:table, :column)"),
            {"table": table_name, "column": column_name},
        )
        return result.scalar_one()

    def quantize_preload(self, table: TableRef, column: ColumnRef) -> None:
        """
        Load quantized representation into memory.

        Should be called at startup for optimal query performance.
        The preloaded data is shared across all database connections.

        Args:
            table: Table class or name.
            column: Column attribute or name.
        """
        table_name, column_name = _resolve_table_and_column(table, column)
        self.session.execute(
            text("SELECT vector_quantize_preload(:table, :column)"),
            {"table": table_name, "column": column_name},
        )

    def quantize_cleanup(self, table: TableRef, column: ColumnRef) -> None:
        """
        Release memory from quantization and remove quantization entries.

        Use when quantization is no longer required. Running VACUUM may be
        necessary to reclaim freed space.

        Args:
            table: Table class or name.
            column: Column attribute or name.
        """
        table_name, column_name = _resolve_table_and_column(table, column)
        self.session.execute(
            text("SELECT vector_quantize_cleanup(:table, :column)"),
            {"table": table_name, "column": column_name},
        )

    # ==================== Search Operations ====================

    def _prepare_query_vector(
        self,
        query_vector: Sequence[float | int] | bytes,
    ) -> str | bytes:
        """Prepare query vector for search operations."""
        if isinstance(query_vector, bytes):
            return query_vector
        return self._vector_to_json(query_vector)

    @overload
    def search(
        self,
        table: TableRef,
        column: ColumnRef,
        query_vector: Sequence[float | int] | bytes,
        k: int,
        *,
        use_quantization: bool = True,
        vector_type: VectorType = VectorType.FLOAT32,
        as_dict: Literal[False] = False,
    ) -> list[VectorSearchResult]: ...

    @overload
    def search(
        self,
        table: TableRef,
        column: ColumnRef,
        query_vector: Sequence[float | int] | bytes,
        k: int,
        *,
        use_quantization: bool = True,
        vector_type: VectorType = VectorType.FLOAT32,
        as_dict: Literal[True],
    ) -> list[dict[str, Any]]: ...

    def search(
        self,
        table: TableRef,
        column: ColumnRef,
        query_vector: Sequence[float | int] | bytes,
        k: int,
        *,
        use_quantization: bool = True,
        vector_type: VectorType = VectorType.FLOAT32,
        as_dict: bool = False,
    ) -> list[VectorSearchResult] | list[dict[str, Any]]:
        """
        Perform nearest neighbor search.

        Args:
            table: Table class or name.
            column: Column attribute or name.
            query_vector: The query vector (as sequence or pre-encoded bytes).
            k: Number of nearest neighbors to return.
            use_quantization: If True, use fast approximate search (requires prior quantization).
                              If False, use brute-force full scan.
            vector_type: Type of the query vector (default: FLOAT32).
            as_dict: If True, return results as dictionaries.

        Returns:
            List of VectorSearchResult or dicts with 'rowid' and 'distance'.

        Example:
            >>> results = vectors.search(
            ...     DocumentTable, DocumentTable.embedding,
            ...     [0.1, 0.2, 0.3], k=10
            ... )
            >>> for r in results:
            ...     print(f"Row {r.rowid}: distance={r.distance}")
        """
        table_name, column_name = _resolve_table_and_column(table, column)
        vec = self._prepare_query_vector(query_vector)
        encode_func = _ENCODE_FUNC_MAP[vector_type]

        scan_func = "vector_quantize_scan" if use_quantization else "vector_full_scan"

        # Build query - use JSON directly in the function call
        if isinstance(vec, bytes):
            query = text(
                f"SELECT rowid, distance FROM {scan_func}(:table, :column, :vec, :k)"
            )
            result = self.session.execute(
                query, {"table": table_name, "column": column_name, "vec": vec, "k": k}
            )
        else:
            query = text(
                f"SELECT rowid, distance FROM {scan_func}(:table, :column, {encode_func}(:vec), :k)"
            )
            result = self.session.execute(
                query, {"table": table_name, "column": column_name, "vec": vec, "k": k}
            )

        rows = result.fetchall()

        if as_dict:
            return [{"rowid": row[0], "distance": row[1]} for row in rows]
        return [VectorSearchResult(rowid=row[0], distance=row[1]) for row in rows]

    def full_scan(
        self,
        table: TableRef,
        column: ColumnRef,
        query_vector: Sequence[float | int] | bytes,
        k: int,
        vector_type: VectorType = VectorType.FLOAT32,
    ) -> list[VectorSearchResult]:
        """
        Perform brute-force nearest neighbor search.

        Despite being brute-force, this is SIMD-optimized and useful for
        small datasets (< 1M rows) or validation.

        Args:
            table: Table class or name.
            column: Column attribute or name.
            query_vector: The query vector.
            k: Number of nearest neighbors to return.
            vector_type: Type of the query vector.

        Returns:
            List of VectorSearchResult with rowid and distance.
        """
        return self.search(
            table,
            column,
            query_vector,
            k,
            use_quantization=False,
            vector_type=vector_type,
        )

    def quantize_scan(
        self,
        table: TableRef,
        column: ColumnRef,
        query_vector: Sequence[float | int] | bytes,
        k: int,
        vector_type: VectorType = VectorType.FLOAT32,
    ) -> list[VectorSearchResult]:
        """
        Perform fast approximate nearest neighbor search using quantization.

        Recommended for large datasets. Handles 1M vectors of dimension 768
        in milliseconds with <50MB RAM and >0.95 recall.

        Args:
            table: Table class or name.
            column: Column attribute or name.
            query_vector: The query vector.
            k: Number of nearest neighbors to return.
            vector_type: Type of the query vector.

        Returns:
            List of VectorSearchResult with rowid and distance.

        Note:
            Requires prior call to quantize().
        """
        return self.search(
            table,
            column,
            query_vector,
            k,
            use_quantization=True,
            vector_type=vector_type,
        )

    # ==================== Streaming Search Operations ====================

    def search_stream(
        self,
        table: TableRef,
        column: ColumnRef,
        query_vector: Sequence[float | int] | bytes,
        *,
        use_quantization: bool = True,
        vector_type: VectorType = VectorType.FLOAT32,
        limit: int | None = None,
        where_clause: str | None = None,
        where_params: dict[str, Any] | None = None,
    ) -> Iterator[VectorSearchResult]:
        """
        Perform streaming nearest neighbor search with optional filtering.

        Unlike regular search, this allows combining vector search with
        SQL WHERE clauses and LIMIT for filtered/progressive results.

        Args:
            table: Table class or name.
            column: Column attribute or name.
            query_vector: The query vector.
            use_quantization: Use approximate search if True.
            vector_type: Type of the query vector.
            limit: Maximum number of results to return.
            where_clause: Optional SQL WHERE clause (without 'WHERE' keyword).
            where_params: Parameters for the WHERE clause.

        Yields:
            VectorSearchResult objects.

        Example:
            >>> for result in vectors.search_stream(
            ...     DocumentTable, DocumentTable.embedding, query_vec,
            ...     limit=10, where_clause="category = :cat",
            ...     where_params={"cat": "science"}
            ... ):
            ...     print(result)
        """
        table_name, column_name = _resolve_table_and_column(table, column)
        vec = self._prepare_query_vector(query_vector)
        encode_func = _ENCODE_FUNC_MAP[vector_type]

        scan_func = (
            "vector_quantize_scan_stream"
            if use_quantization
            else "vector_full_scan_stream"
        )

        # Build query
        params: dict[str, Any] = {"table": table_name, "column": column_name}

        if isinstance(vec, bytes):
            base_query = (
                f"SELECT rowid, distance FROM {scan_func}(:table, :column, :vec)"
            )
            params["vec"] = vec
        else:
            base_query = f"SELECT rowid, distance FROM {scan_func}(:table, :column, {encode_func}(:vec))"
            params["vec"] = vec

        if where_clause:
            base_query += f" WHERE {where_clause}"
            if where_params:
                params.update(where_params)

        if limit is not None:
            base_query += f" LIMIT {int(limit)}"

        result = self.session.execute(text(base_query), params)

        for row in result:
            yield VectorSearchResult(rowid=row[0], distance=row[1])

    def search_with_join(
        self,
        table: TableRef,
        column: ColumnRef,
        query_vector: Sequence[float | int] | bytes,
        k: int,
        select_columns: list[ColumnRef] | None = None,
        *,
        use_quantization: bool = True,
        vector_type: VectorType = VectorType.FLOAT32,
    ) -> list[Row[Any]]:
        """
        Perform search and join with the original table to get additional columns.

        Args:
            table: Table class or name.
            column: Column attribute or name containing vectors.
            query_vector: The query vector.
            k: Number of nearest neighbors to return.
            select_columns: Column attributes or names to select from the original table.
                           If None, selects all columns (*).
            use_quantization: Use approximate search if True.
            vector_type: Type of the query vector.

        Returns:
            List of SQLAlchemy Row objects with vector search results and table columns.

        Example:
            >>> results = vectors.search_with_join(
            ...     DocumentTable, DocumentTable.embedding, query_vec, k=10,
            ...     select_columns=[DocumentTable.id, DocumentTable.title, DocumentTable.content]
            ... )
            >>> for row in results:
            ...     print(f"{row.title}: distance={row.distance}")
        """
        table_name, column_name = _resolve_table_and_column(table, column)
        vec = self._prepare_query_vector(query_vector)
        encode_func = _ENCODE_FUNC_MAP[vector_type]

        scan_func = "vector_quantize_scan" if use_quantization else "vector_full_scan"

        # Build column selection
        if select_columns:
            col_names = [_resolve_column_name(c) for c in select_columns]
            cols = ", ".join(f"{table_name}.{col}" for col in col_names)
        else:
            cols = f"{table_name}.*"

        params: dict[str, Any] = {"table": table_name, "column": column_name, "k": k}

        if isinstance(vec, bytes):
            query = text(f"""
                SELECT v.rowid, v.distance, {cols}
                FROM {scan_func}(:table, :column, :vec, :k) AS v
                JOIN {table_name} ON {table_name}.rowid = v.rowid
            """)
            params["vec"] = vec
        else:
            query = text(f"""
                SELECT v.rowid, v.distance, {cols}
                FROM {scan_func}(:table, :column, {encode_func}(:vec), :k) AS v
                JOIN {table_name} ON {table_name}.rowid = v.rowid
            """)
            params["vec"] = vec

        result = self.session.execute(query, params)
        return list(result.fetchall())

    def search_stream_with_join(
        self,
        table: TableRef,
        column: ColumnRef,
        query_vector: Sequence[float | int] | bytes,
        select_columns: list[ColumnRef] | None = None,
        *,
        use_quantization: bool = True,
        vector_type: VectorType = VectorType.FLOAT32,
        limit: int | None = None,
        where_clause: str | None = None,
        where_params: dict[str, Any] | None = None,
    ) -> Iterator[Row[Any]]:
        """
        Perform streaming search with join for filtered results with additional columns.

        Args:
            table: Table class or name.
            column: Column attribute or name containing vectors.
            query_vector: The query vector.
            select_columns: Column attributes or names to select.
            use_quantization: Use approximate search if True.
            vector_type: Type of the query vector.
            limit: Maximum number of results.
            where_clause: Optional SQL WHERE clause for filtering.
            where_params: Parameters for the WHERE clause.

        Yields:
            SQLAlchemy Row objects.

        Example:
            >>> for row in vectors.search_stream_with_join(
            ...     DocumentTable, DocumentTable.embedding, query_vec,
            ...     select_columns=[DocumentTable.title],
            ...     limit=10,
            ...     where_clause="documents.category = :cat",
            ...     where_params={"cat": "science"}
            ... ):
            ...     print(f"{row.title}: {row.distance}")
        """
        table_name, column_name = _resolve_table_and_column(table, column)
        vec = self._prepare_query_vector(query_vector)
        encode_func = _ENCODE_FUNC_MAP[vector_type]

        scan_func = (
            "vector_quantize_scan_stream"
            if use_quantization
            else "vector_full_scan_stream"
        )

        # Build column selection
        if select_columns:
            col_names = [_resolve_column_name(c) for c in select_columns]
            cols = ", ".join(f"{table_name}.{col}" for col in col_names)
        else:
            cols = f"{table_name}.*"

        params: dict[str, Any] = {"table": table_name, "column": column_name}

        if isinstance(vec, bytes):
            base_query = f"""
                SELECT
                    v.rowid,
                    row_number() OVER (ORDER BY v.distance) AS rank_number,
                    v.distance,
                    {cols}
                FROM {scan_func}(:table, :column, :vec) AS v
                JOIN {table_name} ON {table_name}.rowid = v.rowid
            """
            params["vec"] = vec
        else:
            base_query = f"""
                SELECT
                    v.rowid,
                    row_number() OVER (ORDER BY v.distance) AS rank_number,
                    v.distance,
                    {cols}
                FROM {scan_func}(:table, :column, {encode_func}(:vec)) AS v
                JOIN {table_name} ON {table_name}.rowid = v.rowid
            """
            params["vec"] = vec

        if where_clause:
            base_query += f" WHERE {where_clause}"
            if where_params:
                params.update(where_params)

        if limit is not None:
            base_query += f" LIMIT {int(limit)}"

        result = self.session.execute(text(base_query), params)

        for row in result:
            yield row

    # ==================== ORM-Integrated Search ====================

    def search_models(
        self,
        table: type[T],
        column: ColumnRef,
        query_vector: Sequence[float | int] | bytes,
        k: int,
        *,
        use_quantization: bool = True,
        vector_type: VectorType = VectorType.FLOAT32,
    ) -> list[tuple[T, float]]:
        """
        Perform search and return ORM model instances with their distances.

        This is the most SQLAlchemy-native way to perform vector search,
        returning actual model instances that can be used directly.

        Args:
            table: Table class (DeclarativeBase subclass).
            column: Column attribute containing vectors.
            query_vector: The query vector.
            k: Number of nearest neighbors to return.
            use_quantization: Use approximate search if True.
            vector_type: Type of the query vector.

        Returns:
            List of tuples (model_instance, distance), sorted by distance.

        Example:
            >>> results = vectors.search_models(
            ...     DocumentTable, DocumentTable.embedding,
            ...     query_vec, k=10
            ... )
            >>> for doc, distance in results:
            ...     print(f"{doc.title}: {distance}")
            ...     doc.views += 1  # Can modify the model directly
        """
        table_name, column_name = _resolve_table_and_column(table, column)
        vec = self._prepare_query_vector(query_vector)
        encode_func = _ENCODE_FUNC_MAP[vector_type]

        scan_func = "vector_quantize_scan" if use_quantization else "vector_full_scan"

        # Get primary key column(s)
        pk_cols = [c.name for c in table.__table__.primary_key.columns]  # ty:ignore[unresolved-attribute]
        if not pk_cols:
            raise ValueError(f"Table {table_name} has no primary key defined")

        pk_col = pk_cols[0]  # Use first primary key column

        # Build query that joins vector search with the table to get PK values
        if isinstance(vec, bytes):
            query = text(f"""
                SELECT {table_name}.{pk_col}, v.distance
                FROM {scan_func}(:table, :column, :vec, :k) AS v
                JOIN {table_name} ON {table_name}.rowid = v.rowid
                ORDER BY v.distance
            """)
            result = self.session.execute(
                query, {"table": table_name, "column": column_name, "vec": vec, "k": k}
            )
        else:
            query = text(f"""
                SELECT {table_name}.{pk_col}, v.distance
                FROM {scan_func}(:table, :column, {encode_func}(:vec), :k) AS v
                JOIN {table_name} ON {table_name}.rowid = v.rowid
                ORDER BY v.distance
            """)
            result = self.session.execute(
                query, {"table": table_name, "column": column_name, "vec": vec, "k": k}
            )

        rows = result.fetchall()
        if not rows:
            return []

        # Build mapping of pk -> distance (preserving order)
        pk_to_distance: dict[Any, float] = {}
        pk_order: list[Any] = []
        for row in rows:
            pk_val, distance = row[0], row[1]
            pk_to_distance[pk_val] = distance
            pk_order.append(pk_val)

        # Query ORM for the actual model instances
        pk_attr = getattr(table, pk_col)
        models = self.session.query(table).filter(pk_attr.in_(pk_order)).all()

        # Build pk -> model mapping
        pk_to_model: dict[Any, T] = {}
        for model in models:
            pk_val = getattr(model, pk_col)
            pk_to_model[pk_val] = model

        # Return in distance order
        results: list[tuple[T, float]] = []
        for pk_val in pk_order:
            if pk_val in pk_to_model:
                results.append((pk_to_model[pk_val], pk_to_distance[pk_val]))

        return results


def _load_vector_extension(dbapi_connection, connection_record):
    """
    Load the sqlite-vector extension on a new connection.

    This function is intended to be used as a SQLAlchemy event listener
    for the 'connect' event.
    """
    try:
        # Locate the extension binary provided by the sqlite-vector package
        ext_path = importlib.resources.files("sqlite_vector.binaries") / "vector"

        # The dbapi_connection is typically a sqlite3.Connection object
        dbapi_connection.enable_load_extension(True)
        dbapi_connection.load_extension(str(ext_path))
        dbapi_connection.enable_load_extension(False)
    except Exception as e:
        logging.getLogger(__name__).error(
            f"Failed to load sqlite-vector extension: {e}"
        )


def register_vector_extension(engine: Engine) -> None:
    """
    Register the sqlite-vector extension to be loaded on new connections.

    Args:
        engine: The SQLAlchemy Engine instance.
    """
    event.listen(engine, "connect", _load_vector_extension)
