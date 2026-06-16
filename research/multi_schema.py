"""Multi-database layout for research data.

market.duckdb  — bars, halts, market context
news.duckdb    — raw news items and fetch attempts
signals.duckdb — scanner snapshots and engineered features

All share the same core schema (tables are cheap when empty), which keeps
cross-database queries simple.
"""

from __future__ import annotations

from pathlib import Path

from storage.db import get_connection

RESEARCH_DIR = Path("data/research")

DB_FILES = {
    "market": "market.duckdb",
    "news": "news.duckdb",
    "signals": "signals.duckdb",
}


def research_db_path(name: str, base_dir: str | Path | None = None) -> Path:
    base = Path(base_dir) if base_dir else RESEARCH_DIR
    filename = DB_FILES.get(name, f"{name}.duckdb")
    return base / filename


def open_research_db(name: str, base_dir: str | Path | None = None):
    """Open one of the research databases, creating schema if needed."""
    return get_connection(research_db_path(name, base_dir))
