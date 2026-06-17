"""Event store implementation for Milestone 2.

Append-only event storage with canonical event types.
"""

from .event_schema import (
    BaseEvent,
    EventType,
    EventMode,
)
from .db import get_connection
from uuid import uuid4
import json
import logging
import threading

logger = logging.getLogger(__name__)


class EventStore:
    """Append-only event store for canonical events."""

    def __init__(self, db_path: str = "./data/momentum_events.duckdb"):
        """Initialize event store with DuckDB connection."""
        self.con = get_connection(db_path)
        # serialize DB access: the fast trigger thread and the main loop share
        # this one psycopg2 connection, which is not safe for concurrent use.
        self._lock = threading.RLock()
        self._ensure_schema()

    def _ensure_schema(self):
        """Ensure event tables exist in database."""
        cursor = self.con.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id VARCHAR PRIMARY KEY,
                timestamp TIMESTAMP,
                mode VARCHAR,
                event_type VARCHAR,
                correlation_id VARCHAR,
                message VARCHAR,
                payload_json TEXT,
                created_at TIMESTAMP DEFAULT current_timestamp
            );
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_timestamp
            ON events(timestamp);
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_session_id
            ON events(correlation_id);
        """)
        # No index on payload_json: a btree over full JSON text is invalid for
        # large payloads in Postgres and useless for the LIKE-based symbol
        # filter (query_events scans with payload_json LIKE instead).
        self.con.commit()

    def emit(self, event: BaseEvent) -> str:
        """Emit an event to the event store.

        Args:
            event: Event to emit

        Returns:
            Event ID
        """
        event_id = str(uuid4())
        payload_json = json.dumps(event.model_dump(mode="json", exclude_unset=True))

        with self._lock:
            cursor = self.con.cursor()
            cursor.execute(
                """
                INSERT INTO events
                    (id, timestamp, mode, event_type, correlation_id, message, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, current_timestamp)
                """,
                [
                    event_id,
                    event.timestamp,
                    event.mode.value,
                    event.event_type.value
                    if isinstance(event.event_type, EventType)
                    else str(event.event_type),
                    event.correlation_id,
                    event.message,
                    payload_json,
                ],
            )
            self.con.commit()

        logger.debug(
            f"Emitted event {event_id}: {event.event_type.value} - {event.message}"
        )
        return event_id

    def query_events(
        self,
        event_type: str | None = None,
        symbol: str | None = None,
        correlation_id: str | None = None,
        session_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int | None = 1000,
    ) -> list[dict]:
        """Query events from the store.

        Args:
            event_type: Filter by event type
            symbol: Filter by symbol
            correlation_id: Filter by correlation ID
            session_id: Filter by session ID
            since: Start timestamp (inclusive)
            until: End timestamp (exclusive)
            limit: Maximum number of results

        Returns:
            List of event dictionaries
        """
        conditions = []
        params = []

        if event_type is not None:
            conditions.append("event_type = ?")
            params.append(event_type)

        if symbol is not None:
            conditions.append("payload_json LIKE ?")
            params.append(f"%\"symbol\": \"{symbol}\"%")

        if correlation_id is not None:
            conditions.append("correlation_id = ?")
            params.append(correlation_id)

        if session_id is not None:
            conditions.append("correlation_id = ?")
            params.append(session_id)

        if since is not None:
            conditions.append("timestamp >= ?")
            params.append(since)

        if until is not None:
            conditions.append("timestamp < ?")
            params.append(until)

        where_clause = " AND ".join(conditions) if conditions else "1=1"
        limit_clause = f"LIMIT {limit}" if limit is not None else ""

        query = f"""
            SELECT id, timestamp, mode, event_type, correlation_id, message, payload_json, created_at
            FROM events
            WHERE {where_clause}
            ORDER BY timestamp ASC, created_at ASC
            {limit_clause}
        """

        with self._lock:
            cursor = self.con.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()

        results = []
        for row in rows:
            results.append(
                {
                    "id": row[0],
                    "timestamp": str(row[1]),
                    "mode": row[2],
                    "event_type": row[3],
                    "correlation_id": row[4],
                    "message": row[5],
                    "payload_json": row[6],
                    "created_at": str(row[7]),
                }
            )

        return results

    def count_events(self) -> int:
        """Total number of events. Cheap change-detection signal for SSE."""
        with self._lock:
            cursor = self.con.cursor()
            cursor.execute("SELECT COUNT(*) FROM events")
            row = cursor.fetchone()
        return int(row[0]) if row else 0

    def get_symbol_timeline(self, symbol: str) -> list[dict]:
        """Get full timeline for a symbol.

        Args:
            symbol: Ticker symbol

        Returns:
            List of all events for the symbol in chronological order
        """
        return self.query_events(symbol=symbol, limit=None)

    def rebuild_state(self, session_id: str) -> dict:
        """Rebuild state from event store for a session.

        Args:
            session_id: Session ID to rebuild

        Returns:
            Dictionary with session summary and latest state
        """
        events = self.query_events(session_id=session_id, limit=None)

        summary = {
            "session_id": session_id,
            "total_events": len(events),
            "event_types": {},
        }

        for event in events:
            event_type = event["event_type"]
            summary["event_types"][event_type] = (
                summary["event_types"].get(event_type, 0) + 1
            )

        return summary

    def close(self):
        """Close event store connection."""
        if self.con:
            self.con.close()
