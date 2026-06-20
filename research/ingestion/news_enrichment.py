"""Local-LLM (Ollama) news/catalyst enrichment — advisory layer.

Stdlib-only. Mirrors the proven ``strategy/evaluation/llm_integration.py`` urllib
call but asks Ollama for STRUCTURED JSON (``"format": "json"``) so each headline
yields a parseable catalyst classification.

Design rules (see plan):
  * Runs OFF the hot trading path (called from the interval Scheduler).
  * Every public function degrades gracefully — returns ``None`` / skips on any
    failure (network down, bad JSON, missing keys). Trading is never blocked.
  * Single Postgres datastore: SQL uses DuckDB-style ``?`` placeholders, which
    ``storage/db_pg`` translates to psycopg2 ``%s`` (exactly like rss.py).
"""

from __future__ import annotations

import json
import logging
import re
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Closed vocabulary the model must choose from. Keep in sync with build_prompt.
CATALYST_TYPES = {
    "offering_dilution",
    "earnings",
    "fda_approval",
    "clinical_trial",
    "ma_acquisition",
    "partnership_contract",
    "guidance_update",
    "regulatory",
    "halt_resumption",
    "stock_split_reverse",
    "insider_buyback",
    "analyst_rating",
    "other",
    "none",
}

# Cheap stdlib floor for the dilution veto so it still works when Ollama is down.
# The LLM CONFIRMS dilution; this regex is the safety net, not the primary signal.
_DILUTIVE_RE = re.compile(
    r"\b(offering|registered\s+direct|at[-\s]the[-\s]market|atm\s+(?:facility|offering)|"
    r"warrant|dilut\w*|shelf\s+(?:registration|offering)|priced\s+(?:public\s+)?offering|"
    r"pricing\s+of\s+.{0,30}offering)\b|\bform\s+s-?3\b",
    re.IGNORECASE,
)


def looks_dilutive(text: str) -> bool:
    """Keyword floor for the dilution signal (used when the LLM is unavailable)."""
    return bool(_DILUTIVE_RE.search(text or ""))


@dataclass
class CatalystAnalysis:
    """A model's read of one headline. All fields validated/clamped on parse."""

    catalyst_type: str = "other"
    sentiment: float = 0.0  # [-1, 1] bearish..bullish for the stock
    conviction: float = 0.0  # [0, 1] model confidence it's a real, tradeable catalyst
    is_dilutive: bool = False
    rationale: str = ""

    def as_news_event_fields(self) -> dict:
        """Map to the (previously dead) news_events columns."""
        return {
            "sentiment": self.sentiment,
            "category": self.catalyst_type,
            "is_offering": self.is_dilutive,
            "is_earnings": self.catalyst_type == "earnings",
            "is_halt_related": self.catalyst_type == "halt_resumption",
        }


def build_prompt(headline: str, snippet: str = "", tickers: str = "") -> str:
    """Instruction prompt pinning the enum and demanding a strict JSON object."""
    types = ", ".join(sorted(CATALYST_TYPES))
    ctx = f"Headline: {headline}"
    if snippet:
        ctx += f"\nDetail: {snippet}"
    if tickers:
        ctx += f"\nTickers: {tickers}"
    return (
        "You are a small-cap trading analyst. Classify the market catalyst in this "
        "news for a momentum day-trader. Respond with a STRICT JSON object only, no "
        "prose, with exactly these keys:\n"
        '  "catalyst_type": one of [' + types + "]\n"
        '  "sentiment": number from -1.0 (very bearish) to 1.0 (very bullish) for the stock\n'
        '  "conviction": number from 0.0 to 1.0 — how confident this is a real, tradeable catalyst\n'
        '  "is_dilutive": true ONLY for confirmed share dilution (offering, ATM, '
        "registered direct, warrant, shelf takedown), else false\n"
        '  "rationale": one short sentence (max ~200 chars)\n\n'
        + ctx
    )


def _clamp(value, lo: float, hi: float, default: float) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default


def _parse(raw: str, fallback_text: str = "") -> CatalystAnalysis | None:
    """Tolerant parse of the model's JSON string. None on unrecoverable failure."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    ctype = str(data.get("catalyst_type", "other")).strip().lower()
    if ctype not in CATALYST_TYPES:
        ctype = "other"
    is_dilutive = bool(data.get("is_dilutive", False)) or (
        ctype == "offering_dilution"
    )
    # belt-and-suspenders: trust the keyword floor if the model missed obvious dilution
    if not is_dilutive and looks_dilutive(fallback_text):
        is_dilutive = True
    rationale = str(data.get("rationale", "") or "")[:200]
    return CatalystAnalysis(
        catalyst_type=ctype,
        sentiment=_clamp(data.get("sentiment", 0.0), -1.0, 1.0, 0.0),
        conviction=_clamp(data.get("conviction", 0.0), 0.0, 1.0, 0.0),
        is_dilutive=is_dilutive,
        rationale=rationale,
    )


def classify_headline(
    headline: str,
    snippet: str = "",
    tickers: str = "",
    *,
    host: str = "http://localhost:11434",
    model: str = "qwen2.5:7b-instruct",
    timeout: int = 30,
    temperature: float = 0.3,
    max_tokens: int = 256,
) -> CatalystAnalysis | None:
    """Ask a local Ollama model to classify a headline. None on any failure."""
    if not (headline or snippet):
        return None
    prompt = build_prompt(headline, snippet, tickers)
    try:
        req = urllib.request.Request(
            f"{host}/api/generate",
            data=json.dumps(
                {
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                    "format": "json",
                    "options": {
                        "temperature": temperature,
                        "num_predict": max_tokens,
                    },
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
        response = body.get("response")
    except Exception:  # noqa: BLE001
        logger.debug("catalyst enrichment unavailable", exc_info=True)
        return None
    if not response:
        return None
    return _parse(response, fallback_text=f"{headline} {snippet}")


# ---------------------------------------------------------------------------
# Persistence (single Postgres datastore; ``?`` placeholders, like rss.py)
# ---------------------------------------------------------------------------

def catalyst_score(advisory: dict | None) -> float | None:
    """Map a catalyst advisory dict -> a 0..1 'how bullish a catalyst' score.

    None when there is no advisory. A bearish / no-catalyst read scores low; a
    high-conviction bullish catalyst scores high. Used by Phase 2 to blend into
    the setup quality score (computed OUTSIDE the pure evaluator)."""
    if not advisory:
        return None
    conviction = _clamp(advisory.get("conviction", 0.0), 0.0, 1.0, 0.0)
    sentiment = _clamp(advisory.get("sentiment", 0.0), -1.0, 1.0, 0.0)
    return round(conviction * (0.5 + 0.5 * max(0.0, sentiment)), 4)


def _store_cache_row(con, headline_hash, symbol, headline, source, analysis, model):
    exists = con.execute(
        "SELECT 1 FROM news_catalyst_cache WHERE headline_hash = ? AND symbol = ? LIMIT 1",
        [headline_hash, symbol],
    ).fetchone()
    if exists:
        return
    con.execute(
        "INSERT INTO news_catalyst_cache ("
        "headline_hash, symbol, headline, source, catalyst_type, sentiment, "
        "conviction, is_dilutive, rationale, model"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            headline_hash, symbol, headline, source, analysis.catalyst_type,
            analysis.sentiment, analysis.conviction, analysis.is_dilutive,
            analysis.rationale, model,
        ],
    )


def _upsert_news_event(con, headline_hash, symbol, headline, source, published_at, analysis):
    """Populate the (previously dead) news_events row for this headline+ticker."""
    nid = f"{headline_hash}:{symbol}"
    f = analysis.as_news_event_fields()
    exists = con.execute(
        "SELECT 1 FROM news_events WHERE id = ? LIMIT 1", [nid]
    ).fetchone()
    if exists:
        con.execute(
            "UPDATE news_events SET sentiment = ?, category = ?, is_offering = ?, "
            "is_earnings = ?, is_halt_related = ? WHERE id = ?",
            [f["sentiment"], f["category"], f["is_offering"], f["is_earnings"],
             f["is_halt_related"], nid],
        )
        return
    con.execute(
        "INSERT INTO news_events ("
        "id, symbol, headline, source, published_at, sentiment, category, "
        "is_earnings, is_offering, is_halt_related"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [nid, symbol, headline, source, published_at, f["sentiment"],
         f["category"], f["is_earnings"], f["is_offering"], f["is_halt_related"]],
    )


def enrich_recent_news(con, cfg, lookback_hours: int | None = None, limit: int | None = None) -> dict:
    """Classify recently-ingested, not-yet-enriched headlines via Ollama.

    Left-anti-joins ``raw_news_items`` against ``news_catalyst_cache`` on the
    headline hash, classifies each (one LLM call per headline, fanned out per
    ticker), and writes the cache + populates ``news_events``. Bounded per pass
    by ``limit`` so a backlog cannot run the GPU forever. NEVER raises."""
    lookback_hours = lookback_hours if lookback_hours is not None else getattr(
        cfg, "enrichment_lookback_hours", 12)
    limit = limit if limit is not None else getattr(cfg, "enrichment_batch_limit", 50)
    counts = {"enriched": 0, "skipped": 0, "errors": 0}
    cutoff = datetime.utcnow() - timedelta(hours=lookback_hours)
    try:
        rows = con.execute(
            "SELECT payload_hash, raw_title, raw_body_snippet, raw_tickers, source, "
            "fetched_at FROM raw_news_items "
            "WHERE payload_hash IS NOT NULL AND fetched_at >= ? "
            "AND payload_hash NOT IN (SELECT DISTINCT headline_hash FROM news_catalyst_cache) "
            "ORDER BY fetched_at DESC LIMIT ?",
            [cutoff, limit],
        ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("enrich_recent_news query failed (DB fault?): %s", exc)
        return counts
    for payload_hash, title, snippet, raw_tickers, source, fetched_at in rows:
        analysis = classify_headline(
            title or "", snippet or "", raw_tickers or "",
            host=cfg.host, model=cfg.model, timeout=cfg.timeout_seconds,
            temperature=cfg.temperature, max_tokens=cfg.max_tokens,
        )
        if analysis is None:
            # Ollama down / unparseable — DON'T poison the cache; retry next pass.
            counts["errors"] += 1
            continue
        tickers = [t.strip().upper() for t in (raw_tickers or "").split(",") if t.strip()]
        if not tickers:
            tickers = [""]  # sentinel: marks the headline enriched so we don't re-call
        # All-or-nothing per headline: the connection is autocommit (no real
        # transaction), so on any ticker failure we DELETE the partial rows for
        # this headline. Otherwise the headline-level dedup (NOT IN cache) would
        # exclude it forever, stranding the un-stored tickers; and we'd over-count.
        ok = True
        for sym in tickers:
            try:
                _store_cache_row(con, payload_hash, sym, title, source, analysis, cfg.model)
                if sym:
                    _upsert_news_event(con, payload_hash, sym, title, source, fetched_at, analysis)
            except Exception as exc:  # noqa: BLE001
                ok = False
                counts["errors"] += 1
                logger.debug("catalyst store failed for %s: %s", sym, exc, exc_info=True)
                break
        if ok:
            counts["enriched"] += 1
        else:
            try:  # roll back the partial headline so it fully retries next pass
                con.execute(
                    "DELETE FROM news_catalyst_cache WHERE headline_hash = ?",
                    [payload_hash],
                )
            except Exception:  # noqa: BLE001
                pass
            counts["skipped"] += 1
    return counts


def catalyst_map(con, lookback_hours: int = 8) -> dict[str, dict]:
    """{ticker: advisory} for tickers enriched within the window (latest wins).

    Mirrors ``research.ingestion.discovery.recent_news_map`` — read at arm /
    approval time and (Phase 2) injected into the watcher. Never raises."""
    cutoff = datetime.utcnow() - timedelta(hours=lookback_hours)
    try:
        rows = con.execute(
            "SELECT symbol, catalyst_type, sentiment, conviction, is_dilutive, "
            "rationale, headline FROM news_catalyst_cache "
            "WHERE symbol <> '' AND enriched_at >= ? ORDER BY enriched_at DESC",
            [cutoff],
        ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("catalyst_map query failed (DB fault?): %s", exc)
        return {}
    out: dict[str, dict] = {}
    for sym, ctype, sentiment, conviction, is_dilutive, rationale, headline in rows:
        key = (sym or "").upper()
        if key and key not in out:  # newest first -> first seen is the latest
            out[key] = {
                "catalyst_type": ctype,
                "sentiment": sentiment,
                "conviction": conviction,
                "is_dilutive": bool(is_dilutive),
                "rationale": rationale,
                "headline": headline,
            }
    return out
