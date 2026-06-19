"""Research data namespaces.

Historically three separate DuckDB files (market / news / signals). The datastore
is now a single Postgres database (see :mod:`storage.db_pg`), so these are logical
names that all resolve to the same connection — the layout is retained for
call-site compatibility.
"""

from __future__ import annotations

from pathlib import Path

from storage.db import get_connection

RESEARCH_DIR = Path("data/research")

# Logical research namespaces. The datastore is one Postgres DB and the
# connection layer ignores the path, so all three resolve to the same database.
DB_FILES = {
    "market": "market",
    "news": "news",
    "signals": "signals",
}


def research_db_path(name: str, base_dir: str | Path | None = None) -> Path:
    base = Path(base_dir) if base_dir else RESEARCH_DIR
    filename = DB_FILES.get(name, name)
    return base / filename


def open_research_db(name: str, base_dir: str | Path | None = None):
    """Open the research datastore (Postgres). ``name`` selects a logical
    namespace but currently resolves to the same database."""
    return get_connection(research_db_path(name, base_dir))
