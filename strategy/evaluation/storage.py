"""Persist setup evaluations into the research DB (setup_events table)."""

from __future__ import annotations

import uuid
from datetime import datetime


def store_setup_event(con, symbol: str, session_date, result: dict) -> str:
    """Insert one evaluation result into setup_events. Returns setup_id."""
    setup_id = str(uuid.uuid4())
    setups = result.get("setups") or []
    best = setups[0] if setups else {}
    con.execute(
        """
        INSERT OR REPLACE INTO setup_events
            (setup_id, symbol, setup_time, session_date, setup_name,
             entry_reference_price, invalidation_price, gap_pct, relative_volume)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            setup_id,
            symbol,
            datetime.now(),
            session_date,
            best.get("setup_type") or "first_pullback",
            best.get("entry_price"),
            best.get("stop_loss_price"),
            result.get("gap_pct"),
            result.get("relative_volume"),
        ],
    )
    return setup_id
