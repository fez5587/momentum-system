"""Shared pytest fixtures + session cleanup.

Drop the throwaway ``mem_*`` schemas that ``EventStore(":memory:")`` creates for
test isolation. The closing fixture drops them on ``.close()``, but tests that
build a store inline (without that fixture) leak one schema per run — 170 had
piled up in the shared Postgres instance, bloating the catalog and slowing
query planning. This sweeps them at session end.
"""

import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def _drop_leftover_mem_schemas():
    yield  # run after the whole session
    # Under xdist each worker shares the same Postgres, so a worker dropping at
    # its own session-end could yank a schema another worker is still using.
    # Only sweep in serial runs (the common case); parallel runs can sweep
    # manually afterwards.
    if os.environ.get("PYTEST_XDIST_WORKER"):
        return
    url = os.environ.get("DATABASE_URL")
    if not url:
        return
    try:
        import psycopg2
        conn = psycopg2.connect(url)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT schema_name FROM information_schema.schemata "
                    "WHERE schema_name LIKE 'mem_%'")
        for (schema,) in cur.fetchall():
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        conn.close()
    except Exception:  # noqa: BLE001 — cleanup must never fail the run
        pass
