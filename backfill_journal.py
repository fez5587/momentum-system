#!/usr/bin/env python3
"""One-time backfill of the trade journal from Alpaca's filled-order history.

The live journal (position_closed events from the sync snapshot-diff) is
forward-only, so past market days show 0 trades. This reconstructs every
completed round-trip from order history and emits the matching position_closed
events, timestamped (ET) at the exit fill so each lands on its own market day —
populating per-day win-rate / avg-R / the trade list retroactively.

Idempotent: re-running deletes prior backfill events first. Live-reconciled
closes are left untouched (deduped by symbol + exit-minute).

    python backfill_journal.py --dry-run     # preview, write nothing
    python backfill_journal.py               # write the events
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ET = ZoneInfo("America/New_York")
BACKFILL_TAG = "journal_backfill"


def _to_et_naive(iso: str):
    """Alpaca filled_at is UTC; store as naive ET to match the loop's clock and
    land each close on the correct market day."""
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ET).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="preview, write nothing")
    args = ap.parse_args()

    load_dotenv()
    from alpaca_paper.client import AlpacaPaperClient
    from storage.event_schema import EventMode, PositionClosedEvent
    from storage.event_store import EventStore
    from storage.journal_backfill import flatten_fills, reconstruct_round_trips

    store = EventStore("momentum")
    client = AlpacaPaperClient()

    raw = client.get_orders(status="closed", limit=500, nested=True)
    print(f"fetched {len(raw)} historical orders from Alpaca")
    trips = reconstruct_round_trips(flatten_fills(raw))
    print(f"reconstructed {len(trips)} completed round-trips")

    # existing closes: collect prior-backfill ids (to clear) and live keys (to skip)
    existing = store.query_events(event_type="position_closed", limit=None)
    prior_backfill = [e["id"] for e in existing if e.get("correlation_id") == BACKFILL_TAG]
    live_keys = set()
    for e in existing:
        if e.get("correlation_id") == BACKFILL_TAG:
            continue
        p = json.loads(e["payload_json"])
        live_keys.add((p.get("symbol"), str(e["timestamp"])[:16]))

    pending, by_day, skipped = [], {}, 0
    for t in trips:
        et = _to_et_naive(t["exit_time"])
        if et is None:
            continue
        if (t["symbol"], et.isoformat()[:16]) in live_keys:
            skipped += 1
            continue
        pending.append((t, et))
        d = by_day.setdefault(et.date().isoformat(), {"n": 0, "pnl": 0.0, "w": 0})
        d["n"] += 1
        d["pnl"] += t["realized_pnl"]
        d["w"] += 1 if t["realized_pnl"] > 0 else 0

    print("\nper market day:")
    for day in sorted(by_day):
        d = by_day[day]
        wr = (d["w"] / d["n"] * 100) if d["n"] else 0
        print(f"  {day}:  {d['n']:>3} trades   win {wr:>3.0f}%   pnl {d['pnl']:+.2f}")
    if skipped:
        print(f"(skipped {skipped} already journaled live)")

    if args.dry_run:
        print(f"\n[dry-run] would write {len(pending)} events (no changes made)")
        return

    if prior_backfill:
        store.con.execute(
            "DELETE FROM events WHERE event_type = 'position_closed' "
            "AND correlation_id = ?", [BACKFILL_TAG])
        print(f"cleared {len(prior_backfill)} prior backfill events")

    for t, et in pending:
        store.emit(PositionClosedEvent(
            timestamp=et, mode=EventMode.PAPER, correlation_id=BACKFILL_TAG,
            message=(f"[backfill] {t['symbol']} closed @ {t['exit_price']} "
                     f"({t['exit_reason']}) pnl={t['realized_pnl']:+.2f}"),
            position_id=f"backfill-{t['symbol']}-{et.isoformat()}",
            symbol=t["symbol"], exit_price=t["exit_price"], exit_reason=t["exit_reason"],
            realized_pnl=t["realized_pnl"], entry_price=t["entry_price"],
            stop_loss_price=t["stop_loss_price"], side=t["side"], quantity=t["qty"],
        ))
    print(f"\nemitted {len(pending)} position_closed events across {len(by_day)} market days")


if __name__ == "__main__":
    main()
