"""Database connection management.

The momentum datastore is a single PostgreSQL database. This module preserves
the historical ``get_connection`` / ``get_memory_connection`` entry points but
delegates to :mod:`storage.db_pg`. The ``db_path`` argument is accepted for
call-site compatibility and ignored, except that ":memory:" yields an isolated
throwaway schema (the DuckDB ':memory:' equivalent) so each test gets a fresh
database.
"""

from .db_pg import get_connection, get_memory_connection

__all__ = ["get_connection", "get_memory_connection"]
