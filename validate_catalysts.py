#!/usr/bin/env python3
"""Validate catalyst labels against realized gapper outcomes — BEFORE automating.

For every stored gapper session that ALSO has an enriched news catalyst, this
joins the LLM's label (catalyst_type / sentiment / conviction / is_dilutive) to
the day's realized move (open->close % and open->high %) and reports whether the
labels actually separate winners from losers:

  * do is_dilutive-flagged names fade (lower open->close) vs the rest?
  * do higher bullish catalyst_score names run further?

Run this before flipping NEWS_DILUTION_VETO_ENABLED / NEWS_CATALYST_SCORE_ENABLED.

    PYTHONPATH=. python validate_catalysts.py
    PYTHONPATH=. python validate_catalysts.py --out /tmp/catalyst_validation.csv

IMPORTANT — coverage constraint: bars are retained indefinitely, but news is only
ingested LIVE (RSS feeds can't be replayed for past dates). So a session only has
a catalyst here if the system was running live that day. The report prints how
many sessions actually had both; if that's ~0, let Phase 1 advisory run live for a
stretch first to accumulate news, then re-run.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, time, timedelta

from research.ingestion.news_enrichment import catalyst_score
from research.multi_schema import open_research_db
from learn_params import load_sessions


def _session_outcome(bars) -> dict:
    """open->close % and open->high % from a session's (RTH) minute bars."""
    if bars is None or len(bars) == 0:
        return {"open_to_close_pct": None, "open_to_high_pct": None}
    o = float(bars["open"].iloc[0])
    if o <= 0:
        return {"open_to_close_pct": None, "open_to_high_pct": None}
    close = float(bars["close"].iloc[-1])
    high = float(bars["high"].max())
    return {
        "open_to_close_pct": round((close - o) / o * 100.0, 3),
        "open_to_high_pct": round((high - o) / o * 100.0, 3),
    }


def _catalyst_for(con, symbol: str, session_date) -> dict | None:
    """Latest enriched catalyst for ``symbol`` within +-1 day of the session.

    The +-1 day window ties an overnight/premarket PR to the morning's gap while
    staying tight enough not to borrow a catalyst from a different week."""
    lo = datetime.combine(session_date - timedelta(days=1), time(0, 0))
    hi = datetime.combine(session_date + timedelta(days=1), time(0, 0))
    try:
        row = con.execute(
            "SELECT catalyst_type, sentiment, conviction, is_dilutive, headline "
            "FROM news_catalyst_cache WHERE symbol = ? "
            "AND enriched_at >= ? AND enriched_at < ? "
            "ORDER BY enriched_at DESC LIMIT 1",
            [symbol, lo, hi],
        ).fetchone()
    except Exception:  # noqa: BLE001 — harness must never crash on a DB fault
        return None
    if not row:
        return None
    return {"catalyst_type": row[0], "sentiment": row[1], "conviction": row[2],
            "is_dilutive": bool(row[3]), "headline": row[4]}


def _bucket(score) -> str:
    if score is None:
        return "none"
    if score < 0.33:
        return "low"
    if score < 0.66:
        return "mid"
    return "high"


def _mean(xs):
    return round(sum(xs) / len(xs), 3) if xs else None


def _group_stats(rows: list[dict]) -> dict:
    return {
        "n": len(rows),
        "mean_open_to_close_pct": _mean(
            [r["open_to_close_pct"] for r in rows if r.get("open_to_close_pct") is not None]),
        "mean_open_to_high_pct": _mean(
            [r["open_to_high_pct"] for r in rows if r.get("open_to_high_pct") is not None]),
    }


def outcome_correlation(rows: list[dict]) -> dict:
    """PURE aggregation: split gapper outcomes by dilution flag and score bucket.

    ``rows`` items need: open_to_close_pct, open_to_high_pct, has_catalyst,
    is_dilutive, catalyst_score. No DB/Ollama — unit tested directly."""
    with_cat = [r for r in rows if r.get("has_catalyst")]
    buckets: dict[str, list] = {}
    for r in with_cat:
        buckets.setdefault(_bucket(r.get("catalyst_score")), []).append(r)
    return {
        "total_gappers": len(rows),
        "with_catalyst": len(with_cat),
        "dilutive": _group_stats([r for r in with_cat if r.get("is_dilutive")]),
        "non_dilutive": _group_stats([r for r in with_cat if not r.get("is_dilutive")]),
        "by_score_bucket": {k: _group_stats(v) for k, v in sorted(buckets.items())},
    }


def _format_report(summary: dict, n_sessions: int) -> str:
    def line(label, s):
        c, h = s["mean_open_to_close_pct"], s["mean_open_to_high_pct"]
        c = f"{c:+6.2f}%" if c is not None else "   n/a"
        h = f"{h:+6.2f}%" if h is not None else "   n/a"
        return f"    {label:<14} n={s['n']:<4} open->close {c}   open->high {h}"

    out = [
        "Catalyst → outcome validation",
        f"  sessions with bars: {n_sessions}   gappers: {summary['total_gappers']}   "
        f"with catalyst: {summary['with_catalyst']}",
    ]
    if summary["with_catalyst"] == 0:
        out.append("")
        out.append("  No gapper sessions had an enriched catalyst yet. News is ingested")
        out.append("  LIVE only — let Phase 1 advisory run for a stretch, then re-run this.")
        return "\n".join(out)
    out.append("  Dilution split:")
    out.append(line("dilutive", summary["dilutive"]))
    out.append(line("non-dilutive", summary["non_dilutive"]))
    out.append("  Bullish catalyst_score bucket:")
    for b, s in summary["by_score_bucket"].items():
        out.append(line(b, s))
    return "\n".join(out)


def gather_rows(con, limit_sessions: int | None = None) -> list[dict]:
    sessions = load_sessions(con)
    if limit_sessions:
        sessions = sessions[-limit_sessions:]
    rows = []
    for symbol, sess, bars, _pc, _adv in sessions:
        outcome = _session_outcome(bars)
        cat = _catalyst_for(con, symbol, sess)
        rows.append({
            "symbol": symbol,
            "session_date": str(sess),
            **outcome,
            "has_catalyst": cat is not None,
            "catalyst_type": cat["catalyst_type"] if cat else None,
            "is_dilutive": cat["is_dilutive"] if cat else None,
            "catalyst_score": catalyst_score(cat) if cat else None,
            "headline": cat["headline"] if cat else None,
        })
    return rows


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Validate catalyst labels vs gapper outcomes")
    p.add_argument("--out", default="catalyst_validation.csv", help="CSV output path")
    p.add_argument("--limit-sessions", type=int, default=None,
                   help="only the most recent N (symbol,date) sessions")
    args = p.parse_args(argv)

    con = open_research_db("market")
    rows = gather_rows(con, limit_sessions=args.limit_sessions)
    n_sessions = len({r["session_date"] for r in rows})
    summary = outcome_correlation(rows)
    print(_format_report(summary, n_sessions))

    if rows:
        cols = ["symbol", "session_date", "open_to_close_pct", "open_to_high_pct",
                "has_catalyst", "catalyst_type", "is_dilutive", "catalyst_score", "headline"]
        with open(args.out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k) for k in cols})
        print(f"\n  wrote {len(rows)} rows -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
