import json
from enum import StrEnum
from typing import Any, List, Literal, Optional, Union

from sqlalchemy import Column, Float, Integer, func, literal_column, select, text
from sqlalchemy.orm import DeclarativeBase, Session
from sqlalchemy.sql import Select
from sqlalchemy.sql.functions import GenericFunction


class Distance(StrEnum):
    L2 = "L2"
    SQUARED_L2 = "SQUARED_L2"
    COSINE = "COSINE"
    DOT = "DOT"
    L1 = "L1"


class VectorType(StrEnum):
    FLOAT32 = "FLOAT32"
    FLOAT16 = "FLOAT16"
    FLOATB16 = "FLOATB16"
    INT8 = "INT8"
    UINT8 = "UINT8"


class SQLiteVector:
    """
    Interface for sqlite-vector extension operations.
    """

    @staticmethod
    def version(session: Session) -> str:
        """Returns the current version of the SQLite Vector Extension."""
        return session.execute(select(func.vector_version())).scalar_one()

    @staticmethod
    def backend(session: Session) -> str:
        """Returns the active backend used for vector computation."""
        return session.execute(select(func.vector_backend())).scalar_one()

    @staticmethod
    def init(
        session: Session,
        table: str,
        column: str,
        dimension: int,
        type: VectorType = VectorType.FLOAT32,
        distance: Distance = Distance.L2,
    ) -> None:
        """
        Initializes the vector extension for a given table and column.
        """
        options = f"dimension={dimension},type={type},distance={distance}"
        session.execute(select(func.vector_init(table, column, options)))

    @staticmethod
    def quantize(
        session: Session,
        table: str,
        column: str,
        max_memory: Optional[str] = None,
    ) -> int:
        """
        Performs quantization on the specified table and column.
        Returns the total number of successfully quantized rows.
        """
        options = ""
        if max_memory:
            options = f"max_memory={max_memory}"

        # If options is empty, we just pass the two args, or maybe pass empty string?
        # The doc says options is optional.
        if options:
            return session.execute(
                select(func.vector_quantize(table, column, options))
            ).scalar_one()
        else:
            return session.execute(
                select(func.vector_quantize(table, column))
            ).scalar_one()

    @staticmethod
    def quantize_memory(session: Session, table: str, column: str) -> int:
        """
        Returns the amount of memory (in bytes) required to preload quantized data.
        """
        return session.execute(
            select(func.vector_quantize_memory(table, column))
        ).scalar_one()

    @staticmethod
    def quantize_preload(session: Session, table: str, column: str) -> None:
        """
        Loads the quantized representation for the specified table and column into memory.
        """
        session.execute(select(func.vector_quantize_preload(table, column)))

    @staticmethod
    def quantize_cleanup(session: Session, table: str, column: str) -> None:
        """
        Releases memory previously allocated by a vector_quantize_preload call.
        """
        session.execute(select(func.vector_quantize_cleanup(table, column)))

    @staticmethod
    def as_f32(value: Union[List[float], str]) -> Any:
        """Encodes a vector into the required internal BLOB format (Float32)."""
        if isinstance(value, list):
            value = json.dumps(value)
        return func.vector_as_f32(value)

    @staticmethod
    def as_f16(value: Union[List[float], str]) -> Any:
        """Encodes a vector into the required internal BLOB format (Float16)."""
        if isinstance(value, list):
            value = json.dumps(value)
        return func.vector_as_f16(value)

    @staticmethod
    def as_bf16(value: Union[List[float], str]) -> Any:
        """Encodes a vector into the required internal BLOB format (BFloat16)."""
        if isinstance(value, list):
            value = json.dumps(value)
        return func.vector_as_bf16(value)

    @staticmethod
    def as_i8(value: Union[List[float], str]) -> Any:
        """Encodes a vector into the required internal BLOB format (Int8)."""
        if isinstance(value, list):
            value = json.dumps(value)
        return func.vector_as_i8(value)

    @staticmethod
    def as_u8(value: Union[List[float], str]) -> Any:
        """Encodes a vector into the required internal BLOB format (UInt8)."""
        if isinstance(value, list):
            value = json.dumps(value)
        return func.vector_as_u8(value)

    @classmethod
    def search(
        cls,
        model: Any,
        vector_column: str,
        query_vector: Union[List[float], str],
        k: int,
        quantized: bool = False,
    ) -> Select:
        """
        Constructs a SQLAlchemy Select statement for vector search.

        This method performs a join between the virtual table (vector_full_scan or vector_quantize_scan)
        and the provided model, returning the model instances ordered by distance.

        Note: The model must use the default rowid or have a compatible Integer primary key structure
        for the join to work on 'rowid'.
        """
        table_name = model.__tablename__
        vector_func = func.vector_quantize_scan if quantized else func.vector_full_scan

        # Convert query vector if it's a list, assuming float32 by default
        # If the user wants other types, they should pass the encoded blob or string
        # But for convenience, let's assume f32 if list
        encoded_vector = (
            cls.as_f32(query_vector) if isinstance(query_vector, list) else query_vector
        )

        # The virtual table function call
        scan_query = vector_func(table_name, vector_column, encoded_vector, k).alias(
            "v"
        )

        # Construct the join
        # We assume the model table has a 'rowid' column which matches the virtual table 'rowid'
        # SQLAlchemy models map columns, but SQLite rowid is implicit.
        # We use text("rowid") or the primary key if it's an integer PK that matches rowid semantics.
        # But since we are joining on the table itself, we can use the table object.

        stmt = (
            select(model, scan_query.c.distance)
            .join(
                scan_query, literal_column(f"{table_name}.rowid") == scan_query.c.rowid
            )
            .order_by(scan_query.c.distance)
        )

        return stmt

    @classmethod
    def search_stream(
        cls,
        model: Any,
        vector_column: str,
        query_vector: Union[List[float], str],
        limit: Optional[int] = None,
        quantized: bool = False,
    ) -> Select:
        """
        Constructs a SQLAlchemy Select statement using the streaming interface.
        """
        table_name = model.__tablename__
        vector_func = (
            func.vector_quantize_scan_stream
            if quantized
            else func.vector_full_scan_stream
        )

        encoded_vector = (
            cls.as_f32(query_vector) if isinstance(query_vector, list) else query_vector
        )

        scan_query = vector_func(table_name, vector_column, encoded_vector).alias("v")

        stmt = (
            select(model, scan_query.c.distance)
            .join(
                scan_query, literal_column(f"{table_name}.rowid") == scan_query.c.rowid
            )
            .order_by(scan_query.c.distance)
        )

        if limit:
            stmt = stmt.limit(limit)

        return stmt
