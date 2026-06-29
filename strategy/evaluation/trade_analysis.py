"""Local-LLM (Ollama) AI analysis of trades — advisory layer.

Stdlib-only. Same proven shape as ``research/ingestion/news_enrichment.py``:
ask Ollama for STRUCTURED JSON, parse tolerantly, cache in Postgres, degrade to
a no-op on any failure. Runs OFF the hot trading path (interval Scheduler) and
NEVER gates a trade — every verdict is advisory, surfaced on the dashboard.

Four analyses (``analysis_type``):
  * ``armed``      — a loaded setup: does it still make sense to pursue? (pursue/
                     avoid/monitor + confidence + concerns)
  * ``weak``       — a too-soft setup: why it failed and whether a real thesis remains
  * ``postmortem`` — a closed trade: what went wrong, the management lesson
  * ``eod``        — a once-a-day plain-English session narrative

Design rules mirror the enrichment layer: ``?`` placeholders (translated by
storage/db_pg), bounded per pass by a batch limit, and a ``context_hash`` so an
unchanged setup is not re-sent to the GPU.
"""

from __future__ import annotations

import hashlib
import json
import logging
import urllib.request
from dataclasses import dataclass, field
from datetime import date

logger = logging.getLogger(__name__)

# Verdict vocabulary. 'none' = retrospective/narrative analyses with no call to make.
DECISIONS = {"pursue", "avoid", "monitor", "defer", "none"}

# JSON schema handed to Ollama's ``format`` so decoding is constrained at
# generation time (one parser for all four analyses).
ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": sorted(DECISIONS)},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "summary": {"type": "string"},
        "concerns": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["decision", "confidence", "summary", "concerns"],
}


@dataclass
class TradeAnalysis:
    """The model's read of one setup/trade/session. Validated on parse."""

    decision: str = "none"
    confidence: float = 0.0
    summary: str = ""
    concerns: list[str] = field(default_factory=list)

    def as_row(self) -> dict:
        return {
            "decision": self.decision,
            "confidence": self.confidence,
            "summary": self.summary,
            "concerns": self.concerns,
        }


def _clamp(value, lo: float, hi: float, default: float) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default


def _parse(raw: str) -> TradeAnalysis | None:
    """Tolerant parse of the model's JSON. None on unrecoverable failure."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    decision = str(data.get("decision", "none")).strip().lower()
    if decision not in DECISIONS:
        decision = "none"
    concerns_raw = data.get("concerns", []) or []
    if isinstance(concerns_raw, str):
        concerns_raw = [concerns_raw]
    concerns = [str(c).strip()[:200] for c in concerns_raw if str(c).strip()][:6]
    return TradeAnalysis(
        decision=decision,
        confidence=_clamp(data.get("confidence", 0.0), 0.0, 1.0, 0.0),
        summary=str(data.get("summary", "") or "")[:280],
        concerns=concerns,
    )


def context_hash(payload: dict) -> str:
    """Stable short hash of an analysis input, so an unchanged setup isn't re-sent."""
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Prompt builders (pure — unit tested)
# ---------------------------------------------------------------------------

_CONTRACT = (
    "Respond with a STRICT JSON object only, no prose, with exactly these keys:\n"
    '  "decision": one of [avoid, defer, monitor, none, pursue]\n'
    '  "confidence": number 0.0..1.0\n'
    '  "summary": one short sentence (max ~280 chars)\n'
    '  "concerns": array of short strings (0-6 items)\n\n'
)


def build_armed_prompt(ctx: dict) -> str:
    return (
        "You are a small-cap momentum day-trading coach. A gap-and-go setup is "
        "ARMED (loaded to fire on an opening-range breakout). Judge whether it "
        "still makes sense to PURSUE as a long, or to AVOID (e.g. dilutive/over-"
        "extended/thin) or MONITOR. Weigh the gap, relative volume, range, "
        "distance to trigger, and the catalyst together.\n\n"
        + _CONTRACT
        + "Setup:\n" + json.dumps(ctx, default=str)
    )


def build_weak_prompt(ctx: dict) -> str:
    return (
        "You are a small-cap momentum day-trading coach. This gap setup is too "
        "WEAK to fire (soft gap / low relative volume / narrow range). Explain "
        "briefly WHY it's weak and whether there's still a real thesis worth "
        "watching for a re-trigger. Use decision MONITOR if worth watching, "
        "DEFER if not now, AVOID if it's a trap.\n\n"
        + _CONTRACT
        + "Setup:\n" + json.dumps(ctx, default=str)
    )


def build_postmortem_prompt(ctx: dict) -> str:
    return (
        "You are a trading-performance coach. Review this CLOSED day-trade "
        "(r_multiple = realized R, exit_reason = how it ended). In the summary "
        "give the single biggest management lesson; list concerns as concrete, "
        "actionable lessons. Use decision 'none' (this is retrospective).\n\n"
        + _CONTRACT
        + "Trade:\n" + json.dumps(ctx, default=str)
    )


def build_eod_prompt(ctx: dict) -> str:
    return (
        "You are a trading-desk analyst. Write a brief END-OF-DAY note over the "
        "day's armed names and closed trades: what worked, what didn't, and "
        "patterns to watch tomorrow. Summary = the narrative; concerns = the key "
        "takeaways. Use decision 'none'.\n\n"
        + _CONTRACT
        + "Session:\n" + json.dumps(ctx, default=str)
    )


# ---------------------------------------------------------------------------
# Ollama call
# ---------------------------------------------------------------------------

def _call(prompt: str, cfg, *, use_schema: bool = True) -> TradeAnalysis | None:
    """POST a prompt to Ollama and parse the structured reply. None on any failure."""
    fmt = ANALYSIS_SCHEMA if use_schema else "json"
    try:
        req = urllib.request.Request(
            f"{cfg.host}/api/generate",
            data=json.dumps(
                {
                    "model": cfg.model,
                    "prompt": prompt,
                    "stream": False,
                    "format": fmt,
                    "options": {
                        "temperature": getattr(cfg, "temperature", 0.3),
                        "num_predict": getattr(cfg, "max_tokens", 256),
                    },
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=getattr(cfg, "timeout_seconds", 30)) as resp:
            body = json.loads(resp.read().decode())
        response = body.get("response")
    except Exception:  # noqa: BLE001
        logger.debug("trade analysis unavailable", exc_info=True)
        return None
    return _parse(response) if response else None


def analyze_armed_setup(ctx: dict, cfg) -> TradeAnalysis | None:
    return _call(build_armed_prompt(ctx), cfg)


def analyze_weak_setup(ctx: dict, cfg) -> TradeAnalysis | None:
    return _call(build_weak_prompt(ctx), cfg)


def analyze_closed_trade(ctx: dict, cfg) -> TradeAnalysis | None:
    return _call(build_postmortem_prompt(ctx), cfg)


def analyze_session(ctx: dict, cfg) -> TradeAnalysis | None:
    return _call(build_eod_prompt(ctx), cfg)


# ---------------------------------------------------------------------------
# Persistence (single Postgres datastore; ``?`` placeholders)
# ---------------------------------------------------------------------------

def _cached_hash(con, analysis_type: str, symbol: str, session_date) -> str | None:
    row = con.execute(
        "SELECT context_hash FROM ai_trade_analysis_cache "
        "WHERE analysis_type = ? AND symbol = ? AND session_date = ? LIMIT 1",
        [analysis_type, symbol, session_date],
    ).fetchone()
    return row[0] if row else None


def _store_analysis(con, analysis_type, symbol, session_date, chash, analysis, detail, model):
    concerns = json.dumps(analysis.concerns)
    detail_json = json.dumps(detail, default=str)
    exists = con.execute(
        "SELECT 1 FROM ai_trade_analysis_cache "
        "WHERE analysis_type = ? AND symbol = ? AND session_date = ? LIMIT 1",
        [analysis_type, symbol, session_date],
    ).fetchone()
    if exists:
        con.execute(
            "UPDATE ai_trade_analysis_cache SET context_hash = ?, decision = ?, "
            "confidence = ?, summary = ?, concerns = ?, detail = ?, model = ?, "
            "analyzed_at = current_timestamp "
            "WHERE analysis_type = ? AND symbol = ? AND session_date = ?",
            [chash, analysis.decision, analysis.confidence, analysis.summary,
             concerns, detail_json, model, analysis_type, symbol, session_date],
        )
        return
    con.execute(
        "INSERT INTO ai_trade_analysis_cache ("
        "analysis_type, symbol, session_date, context_hash, decision, confidence, "
        "summary, concerns, detail, model) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [analysis_type, symbol, session_date, chash, analysis.decision,
         analysis.confidence, analysis.summary, concerns, detail_json, model],
    )


def _armed_context(row: dict) -> dict:
    """Pluck the decision-relevant fields off an ArmedTrigger snapshot row."""
    return {
        "symbol": row.get("symbol"),
        "gap_pct": row.get("gap"),
        "rvol": row.get("rvol"),
        "trigger": row.get("trigger"),
        "stop": row.get("stop"),
        "range_pct": row.get("range_pct"),
        "dist_to_trigger": row.get("dist"),
        "catalyst": row.get("catalyst") or "",
    }


def run_trade_analysis(con, snapshot: list[dict], cfg, session_date, limit: int | None = None) -> dict:
    """Analyze armed + weak setups from a trigger-book snapshot. NEVER raises.

    Skips a setup whose context is unchanged since the last pass (context_hash),
    so a stable board doesn't keep hitting the GPU. Bounded by ``limit``."""
    limit = limit if limit is not None else getattr(cfg, "trade_analysis_batch_limit", 8)
    counts = {"analyzed": 0, "skipped": 0, "errors": 0}
    rows = [r for r in (snapshot or []) if r.get("state") in ("armed", "weak")]
    for row in rows[:limit]:
        atype = row["state"]  # 'armed' | 'weak'
        sym = (row.get("symbol") or "").upper()
        if not sym:
            continue
        ctx = _armed_context(row)
        chash = context_hash({"t": atype, **ctx})
        try:
            if _cached_hash(con, atype, sym, session_date) == chash:
                counts["skipped"] += 1
                continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("trade analysis cache read failed: %s", exc)
            return counts
        analysis = (analyze_armed_setup if atype == "armed" else analyze_weak_setup)(ctx, cfg)
        if analysis is None:
            counts["errors"] += 1  # don't poison cache; retry next pass
            continue
        try:
            _store_analysis(con, atype, sym, session_date, chash, analysis, ctx, cfg.model)
            counts["analyzed"] += 1
        except Exception as exc:  # noqa: BLE001
            counts["errors"] += 1
            logger.debug("trade analysis store failed: %s", exc, exc_info=True)
    return counts


def run_closed_trade_analysis(con, trades: list[dict], cfg, session_date, limit: int | None = None) -> dict:
    """Post-mortem each closed trade (dedup by outcome). NEVER raises."""
    limit = limit if limit is not None else getattr(cfg, "trade_analysis_batch_limit", 8)
    counts = {"analyzed": 0, "skipped": 0, "errors": 0}
    for t in (trades or [])[:limit]:
        sym = (t.get("symbol") or "").upper()
        if not sym:
            continue
        ctx = {
            "symbol": sym,
            "r_multiple": t.get("r_multiple"),
            "realized_pnl": t.get("realized_pnl"),
            "exit_reason": t.get("exit_reason"),
        }
        chash = context_hash({"t": "postmortem", **ctx})
        try:
            if _cached_hash(con, "postmortem", sym, session_date) == chash:
                counts["skipped"] += 1
                continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("postmortem cache read failed: %s", exc)
            return counts
        analysis = analyze_closed_trade(ctx, cfg)
        if analysis is None:
            counts["errors"] += 1
            continue
        try:
            _store_analysis(con, "postmortem", sym, session_date, chash, analysis, ctx, cfg.model)
            counts["analyzed"] += 1
        except Exception as exc:  # noqa: BLE001
            counts["errors"] += 1
            logger.debug("postmortem store failed: %s", exc, exc_info=True)
    return counts


def run_session_narrative(con, snapshot: list[dict], trades: list[dict], cfg, session_date) -> dict:
    """One end-of-day note over the day's armed names + closed trades. NEVER raises."""
    counts = {"analyzed": 0, "skipped": 0, "errors": 0}
    armed = [r.get("symbol") for r in (snapshot or []) if r.get("state") in ("armed", "fired", "filled")]
    ctx = {
        "armed_symbols": armed,
        "trades": [{"symbol": t.get("symbol"), "r_multiple": t.get("r_multiple"),
                    "exit_reason": t.get("exit_reason")} for t in (trades or [])],
    }
    chash = context_hash({"t": "eod", **ctx})
    try:
        if _cached_hash(con, "eod", "", session_date) == chash:
            counts["skipped"] += 1
            return counts
    except Exception as exc:  # noqa: BLE001
        logger.warning("eod cache read failed: %s", exc)
        return counts
    analysis = analyze_session(ctx, cfg)
    if analysis is None:
        counts["errors"] += 1
        return counts
    try:
        _store_analysis(con, "eod", "", session_date, chash, analysis, ctx, cfg.model)
        counts["analyzed"] += 1
    except Exception as exc:  # noqa: BLE001
        counts["errors"] += 1
        logger.debug("eod store failed: %s", exc, exc_info=True)
    return counts


def trade_analysis_map(con, session_date) -> dict:
    """{analysis_type: {symbol: advisory}} for a session. Never raises.

    Read model for the dashboard. ``eod`` lands under the '' symbol key."""
    try:
        rows = con.execute(
            "SELECT analysis_type, symbol, decision, confidence, summary, concerns "
            "FROM ai_trade_analysis_cache WHERE session_date = ? "
            "ORDER BY analyzed_at DESC",
            [session_date],
        ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("trade_analysis_map query failed: %s", exc)
        return {}
    out: dict[str, dict] = {}
    for atype, sym, decision, confidence, summary, concerns in rows:
        bucket = out.setdefault(atype, {})
        key = (sym or "").upper()
        if key in bucket:  # newest first -> keep latest per symbol
            continue
        try:
            concerns_list = json.loads(concerns) if concerns else []
        except (TypeError, ValueError):
            concerns_list = []
        bucket[key] = {
            "decision": decision,
            "confidence": round(float(confidence), 3) if confidence is not None else None,
            "summary": summary,
            "concerns": concerns_list,
        }
    return out
