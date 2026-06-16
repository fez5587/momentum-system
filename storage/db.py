"""DuckDB connection management."""

import importlib
from pathlib import Path
from typing import Any

from .schema import create_schema


def get_connection(
    db_path: str | Path = "./data/momentum.duckdb",
) -> Any:
    """Get a DuckDB connection, creating schema if needed."""
    duckdb = importlib.import_module("duckdb")

    if str(db_path) == ":memory:":
        con = duckdb.connect(":memory:")
        create_schema(con)
        return con

    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    create_schema(con)
    return con


def get_memory_connection() -> Any:
    """Get an in-memory DuckDB connection for testing."""
    duckdb = importlib.import_module("duckdb")

    con = duckdb.connect(":memory:")
    create_schema(con)
    return con
