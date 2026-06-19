"""PostgreSQL connection layer for the momentum single datastore.

A thin DB-API compatibility shim over psycopg2 that mimics the small slice of
DuckDB's connection API the codebase actually uses:

    con.execute(sql, params).fetchall() / .fetchone() / .df()
    con.executemany(sql, rows)
    con.cursor().execute(sql, params).fetchall()
    con.commit() / con.close()

so the existing call sites in research/query.py, storage/event_store.py and the
ingestion upserts keep working with minimal change. This is deliberately NOT a
swappable-backend abstraction or an ORM — it is Postgres-only, and exists only
to absorb two mechanical dialect differences:

  * placeholders: callers write DuckDB-style ``?``; we rewrite to psycopg2 ``%s``
    (escaping any literal ``%`` to ``%%`` first).
  * auto-persist: DuckDB persists each statement immediately. We set
    ``autocommit = True`` so the same call sites (some of which never call
    commit) behave identically; EventStore's explicit commit() becomes a no-op.

Connection config comes from the environment (loaded from .env by the app):
DATABASE_URL, or PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE. For local dev/tests
PGHOST defaults to the unix-socket Postgres at /home/philip/pgrun.

Test isolation: a db_path of ":memory:" (what the DuckDB tests passed for a
fresh database) maps to a throwaway Postgres schema that is dropped on close —
giving each test the same isolation DuckDB ":memory:" gave it.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import pandas as pd
import psycopg2

SCHEMA_SQL_PATH = Path(__file__).with_name("pg_schema.sql")


def _translate(sql: str) -> str:
    """DuckDB ``?`` placeholders -> psycopg2 ``%s`` (escaping literal ``%``)."""
    return sql.replace("%", "%%").replace("?", "%s")


def _schema_sql() -> str:
    return SCHEMA_SQL_PATH.read_text()


class _Result:
    """Wraps a cursor so DuckDB-style ``.execute(...).fetchall()/.df()`` works."""

    def __init__(self, cur):
        self._cur = cur

    def fetchall(self) -> list[tuple]:
        return self._cur.fetchall()

    def fetchone(self):
        return self._cur.fetchone()

    def df(self) -> pd.DataFrame:
        cols = [d[0] for d in self._cur.description] if self._cur.description else []
        return pd.DataFrame(self._cur.fetchall(), columns=cols)


class _Cursor:
    """psycopg2 cursor facade that translates ``?`` placeholders."""

    def __init__(self, cur):
        self._cur = cur

    def execute(self, sql: str, params: Any = None) -> "_Cursor":
        self._cur.execute(_translate(sql), params)
        return self

    def executemany(self, sql: str, seq) -> "_Cursor":
        self._cur.executemany(_translate(sql), list(seq))
        return self

    def fetchall(self) -> list[tuple]:
        return self._cur.fetchall()

    def fetchone(self):
        return self._cur.fetchone()

    @property
    def description(self):
        return self._cur.description

    def close(self) -> None:
        self._cur.close()


class PgConnection:
    """DuckDB-ish facade over a psycopg2 connection (autocommit)."""

    def __init__(self, conn, temp_schema: str | None = None):
        self._conn = conn
        self._temp_schema = temp_schema

    def _reconnect(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
        self._conn = _connect()

    def _run(self, fn):
        """Run a DB op, reconnecting ONCE on a dropped connection.

        A long-lived connection (e.g. the `momentum watch` board) gets closed by
        the server on idle/restart; without this the next query raised
        OperationalError and crashed. Isolated test schemas (temp_schema) can't
        be safely re-created, so they don't auto-reconnect.
        """
        try:
            return fn()
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            if self._temp_schema:
                raise
            self._reconnect()
            return fn()

    # --- DuckDB-style convenience -------------------------------------
    def execute(self, sql: str, params: Any = None) -> _Result:
        def go():
            cur = self._conn.cursor()
            cur.execute(_translate(sql), params)
            return _Result(cur)
        return self._run(go)

    def executemany(self, sql: str, seq) -> "PgConnection":
        seq = list(seq)

        def go():
            cur = self._conn.cursor()
            cur.executemany(_translate(sql), seq)
            cur.close()
            return self
        return self._run(go)

    # --- psycopg2-style passthrough -----------------------------------
    def cursor(self) -> _Cursor:
        return self._run(lambda: _Cursor(self._conn.cursor()))

    def commit(self) -> None:  # no-op under autocommit; kept for callers
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        if self._temp_schema:
            try:
                cur = self._conn.cursor()
                cur.execute(f'DROP SCHEMA IF EXISTS "{self._temp_schema}" CASCADE')
                cur.close()
            except Exception:
                pass
        self._conn.close()

    @property
    def raw(self):
        return self._conn


def _connect():
    dsn = os.environ.get("DATABASE_URL")
    if dsn:
        conn = psycopg2.connect(dsn)
    else:
        conn = psycopg2.connect(
            host=os.environ.get("PGHOST", "/home/philip/pgrun"),
            port=os.environ.get("PGPORT", "5432"),
            user=os.environ.get("PGUSER", "admin"),
            password=os.environ.get("PGPASSWORD", ""),
            dbname=os.environ.get("PGDATABASE", "momentum"),
        )
    conn.autocommit = True
    return conn


def _apply_schema(conn, search_path: str | None = None) -> None:
    cur = conn.cursor()
    if search_path:
        cur.execute(f'SET search_path TO "{search_path}"')
    cur.execute(_schema_sql())
    cur.close()


def _isolated_connection() -> PgConnection:
    """A throwaway schema for test isolation (DuckDB ':memory:' equivalent)."""
    conn = _connect()
    schema = f"mem_{uuid.uuid4().hex[:12]}"
    cur = conn.cursor()
    cur.execute(f'CREATE SCHEMA "{schema}"')
    cur.execute(f'SET search_path TO "{schema}"')
    cur.close()
    _apply_schema(conn, search_path=schema)
    return PgConnection(conn, temp_schema=schema)


def get_connection(db_path: str | Path = "momentum") -> PgConnection:
    """Get a Postgres connection, creating the schema if needed.

    ``db_path`` is retained for call-site compatibility but the datastore is a
    single Postgres database. The special value ":memory:" yields an isolated
    throwaway schema (used by the test suite for a fresh database per test).

    ``MOMENTUM_PG_SCHEMA`` (set only by the test harness) pins every connection
    to one named schema, so a test that seeds via one EventStore and reads via
    another (e.g. the dashboard server fixture) shares an isolated database
    instead of falling through to the production ``public`` schema. The fixture
    that sets it owns cleanup, so the connection does not auto-drop the schema.
    """
    if str(db_path) == ":memory:":
        return _isolated_connection()
    conn = _connect()
    pinned = os.environ.get("MOMENTUM_PG_SCHEMA", "").strip()
    if pinned:
        cur = conn.cursor()
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{pinned}"')
        cur.execute(f'SET search_path TO "{pinned}"')
        cur.close()
        _apply_schema(conn, search_path=pinned)
        return PgConnection(conn)
    if os.environ.get("PG_SKIP_SCHEMA", "").strip().lower() not in {"1", "true", "yes"}:
        _apply_schema(conn)
    return PgConnection(conn)


def get_memory_connection() -> PgConnection:
    """Isolated throwaway-schema connection for testing."""
    return _isolated_connection()
