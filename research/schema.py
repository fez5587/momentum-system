"""Research database schema — reuses the core market schema."""

from __future__ import annotations

from pathlib import Path

from storage.db import get_connection

DEFAULT_RESEARCH_DB = "data/research/market.duckdb"


def get_research_connection(db_path: str | Path = DEFAULT_RESEARCH_DB):
    """Open (and initialize) the research market database."""
    return get_connection(db_path)
