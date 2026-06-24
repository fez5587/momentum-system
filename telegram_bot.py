#!/usr/bin/env python3
"""Standalone Telegram Q&A bot — answers free-form questions about the trading
data, READ-ONLY, locked to the authorized chat, Ollama-backed.

Runs as its OWN process (`python telegram_bot.py`) so it can NEVER impede the
trading loop. It long-polls getUpdates; for each message from TELEGRAM_CHAT_ID it
RETRIEVES the relevant data from Postgres (a desk snapshot, plus a focused
per-symbol dive when the question names a ticker) and asks the local Ollama model
to answer. Falls back to the raw snapshot if Ollama is down. Never accepts trade
commands. Sends a help menu so the user knows what it can answer (direction).

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (required), DATABASE_URL,
OLLAMA_HOST / OLLAMA_MODEL / OLLAMA_TIMEOUT_SECONDS / OLLAMA_ENABLED.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request
from collections import Counter

logger = logging.getLogger("telegram_bot")

_API = "https://api.telegram.org"
_ACTION_WORDS = ("buy ", "sell ", "flatten", "close all", "cancel ", "short ",
                 "liquidate", "exit all", "place order", "submit order", "go long")
_HELP_WORDS = ("help", "/help", "/start", "what can you", "what do you", "commands", "menu")
_STOPWORDS = {"P&L", "PNL", "EOD", "VWAP", "ORB", "USD", "AI", "OK", "ET", "AM", "PM",
              "WHY", "HOW", "THE", "AND", "ARE", "WAS", "DID", "ANY", "ALL", "NOT",
              "YOU", "OUR", "WHAT", "WHEN", "BEST", "WORST", "TODAY", "NOW", "R"}

HELP_TEXT = (
    "\U0001F916 I'm your momentum desk assistant (read-only). Ask me in plain English about:\n"
    "• today's P&L / trades — \"how did we do?\", \"best and worst trade?\"\n"
    "• the book — \"what's open?\", \"how's PLUG doing?\"\n"
    "• the watchlist — \"what's on the watchlist?\", \"anything ready?\"\n"
    "• why we are / aren't trading — \"why no entries?\", \"what got blocked?\"\n"
    "• a specific ticker — \"what happened with VTAK?\", \"why did we skip AMC?\"\n"
    "• all-time — \"how are we doing overall?\"\n"
    "I can't place or change trades — questions only."
)


def is_help(q: str) -> bool:
    ql = q.strip().lower()
    return ql in ("help", "/help", "/start", "menu") or any(w in ql for w in _HELP_WORDS)


# --------------------------------------------------------------- data retrieval
def _recent_closed(store, limit=12):
    rows = []
    for e in store.query_events(event_type="position_closed", limit=None):
        p = json.loads(e["payload_json"])
        rows.append((str(e["timestamp"]), p))
    rows.sort(reverse=True)
    return [p for _, p in rows[:limit]]


def known_symbols(store) -> set:
    syms = set()
    try:
        for w in store.query_events(event_type="symbol_state_changed", limit=None):
            s = json.loads(w["payload_json"]).get("symbol")
            if s:
                syms.add(str(s).upper())
    except Exception:  # noqa: BLE001
        pass
    return syms


def gather_context(store) -> str:
    from storage.projections import (query_account_positions_snapshot,
                                     query_session_pnl, query_alltime_score,
                                     query_watch_states_snapshot,
                                     query_ready_signals_snapshot)
    L: list[str] = []
    try:
        p = query_session_pnl(store)
        L.append(f"TODAY (live session) P&L: total {p.get('total_pnl', 0):+.0f} "
                 f"(realized {p.get('realized_pnl', 0):+.0f}, unrealized {p.get('unrealized_pnl', 0):+.0f}); "
                 f"closed {p.get('closed_trades', 0)} W/L {p.get('wins', 0)}/{p.get('losses', 0)}.")
    except Exception as exc:  # noqa: BLE001
        L.append(f"(today P&L unavailable: {exc})")
    try:
        rc = _recent_closed(store, 12)
        if rc:
            L.append("RECENT CLOSED TRADES (newest first):")
            for t in rc:
                L.append(f"  {t.get('symbol')}: {t.get('realized_pnl', 0):+.0f} "
                         f"@entry {t.get('entry_price')} ({t.get('exit_reason', '')})")
    except Exception:  # noqa: BLE001
        pass
    try:
        snap = query_account_positions_snapshot(store)
        positions = snap[0]["positions"] if snap else []
        if positions:
            L.append(f"OPEN POSITIONS ({len(positions)}):")
            for q in positions:
                L.append(f"  {q.get('symbol')}: qty {q.get('quantity')} @ {q.get('avg_entry_price')} "
                         f"-> {q.get('current_price')} (uPL {q.get('unrealized_pnl', 0):+.0f})")
        else:
            L.append("OPEN POSITIONS: none (book is flat).")
    except Exception:  # noqa: BLE001
        pass
    try:
        ws = query_watch_states_snapshot(store)
        if ws:
            by_state = Counter(str(w.get("state") or "unknown") for w in ws)
            L.append(f"WATCHLIST: {len(ws)} symbols — " +
                     ", ".join(f"{n} {st}" for st, n in by_state.most_common()))
            hot = [w for w in ws if str(w.get("state")) in ("ready", "armed")]
            for w in sorted(hot, key=lambda x: -(x.get("last_score") or 0))[:8]:
                L.append(f"  {w.get('symbol')}: {w.get('state')} (score {w.get('last_score')})")
    except Exception:  # noqa: BLE001
        pass
    try:
        rs = query_ready_signals_snapshot(store)
        if rs:
            L.append(f"READY SIGNALS ({len(rs)}):")
            for s in rs[:8]:
                av = s.get("above_vwap")
                L.append(f"  {s.get('symbol')}: entry {s.get('entry_price')} stop {s.get('stop_loss_price')} "
                         f"vwap {s.get('vwap')} {'(above VWAP)' if av else '(BELOW VWAP)' if av is False else ''}")
    except Exception:  # noqa: BLE001
        pass
    try:  # why we are/aren't entering — the recent block tally
        blocks = Counter()
        for e in store.query_events(event_type="risk_rule_triggered", limit=300):
            blocks[json.loads(e["payload_json"]).get("rule_type")] += 1
        if blocks:
            L.append("RECENT ENTRY BLOCKS (why entries were skipped): " +
                     ", ".join(f"{rt}×{n}" for rt, n in blocks.most_common(8)))
    except Exception:  # noqa: BLE001
        pass
    try:
        a = query_alltime_score(store)
        L.append(f"ALL-TIME: realized {a.get('total_realized', 0):+.0f} over "
                 f"{a.get('trading_days', '?')} days, win% {(a.get('win_rate') or 0)*100:.0f}, "
                 f"trades {a.get('trades', '?')}.")
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(L)


def symbol_context(store, sym: str) -> str:
    from storage.projections import query_symbol_criteria
    L = [f"\n=== DETAIL: {sym} ==="]
    try:
        for t in _recent_closed(store, 50):
            if str(t.get("symbol")).upper() == sym:
                L.append(f"last trade: {t.get('realized_pnl', 0):+.0f} @entry {t.get('entry_price')} "
                         f"stop {t.get('stop_loss_price')} ({t.get('exit_reason', '')})")
                break
    except Exception:  # noqa: BLE001
        pass
    try:
        c = query_symbol_criteria(store, sym)
        crit = c.get("criteria") or []
        if crit:
            failed = [x.get("key") or x.get("label") for x in crit if not x.get("passed")]
            passed = [x.get("key") or x.get("label") for x in crit if x.get("passed")]
            L.append(f"setup score {c.get('score')} ({c.get('passed_count')} criteria passed); "
                     f"passed: {', '.join(passed) or 'none'}; failed: {', '.join(failed) or 'none'}")
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(L) if len(L) > 1 else ""


# ---------------------------------------------------------------------- ollama
def ask_ollama(question: str, context: str, *, host: str, model: str,
               timeout: int) -> str | None:
    prompt = (
        "You are the assistant for a small-cap momentum DAY-TRADING bot (opening-range "
        "breakouts on $1-20 gappers). Answer the user's question using ONLY the DATA below, "
        "which is the bot's live state. Be concrete and brief (a few sentences). Quote the "
        "real numbers. If the data doesn't contain the answer, say so and tell the user what "
        "you DO have (P&L, trades, the watchlist, ready signals, entry blocks, per-symbol "
        "setup detail). Never invent tickers or numbers.\n\n"
        f"=== DATA ===\n{context}\n\n=== QUESTION ===\n{question}\n\n=== ANSWER ===\n")
    try:
        req = urllib.request.Request(
            f"{host}/api/generate",
            data=json.dumps({"model": model, "prompt": prompt, "stream": False,
                             "options": {"temperature": 0.2, "num_predict": 500}}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = (json.loads(resp.read().decode()).get("response") or "").strip()
        return text or None
    except Exception:  # noqa: BLE001
        logger.debug("ollama unavailable", exc_info=True)
        return None


def answer(store, question: str, *, host: str, model: str, timeout: int,
           ollama_enabled: bool = True) -> str:
    if is_help(question):
        return HELP_TEXT
    if any(w in (" " + question.lower() + " ") for w in _ACTION_WORDS):
        return ("I'm read-only — I answer questions about the data but won't place or "
                "change trades. Try \"how did we do today?\" or send \"help\".")
    context = gather_context(store)
    # focused per-symbol retrieval: tickers in the question that the bot actually knows
    tokens = set(re.findall(r"\b[A-Z]{1,5}\b", question.upper())) - _STOPWORDS
    known = known_symbols(store)
    for sym in [t for t in tokens if t in known][:3]:
        context += symbol_context(store, sym)
    if ollama_enabled:
        out = ask_ollama(question, context, host=host, model=model, timeout=timeout)
        if out:
            return out
    return f"(LLM unavailable — here's the raw snapshot)\n{context}"


# ------------------------------------------------------------------- transport
def is_authorized(chat_id, allowed) -> bool:
    return allowed is not None and str(chat_id) == str(allowed)


def _send(token: str, chat_id, text: str) -> None:
    try:
        req = urllib.request.Request(
            f"{_API}/bot{token}/sendMessage",
            data=json.dumps({"chat_id": chat_id, "text": text[:4000]}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:  # noqa: BLE001
        logger.warning("reply send failed", exc_info=True)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    allowed = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not allowed:
        logger.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — Q&A bot disabled")
        return
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    model = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b-instruct")
    timeout = int(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "60"))
    ollama_enabled = os.environ.get("OLLAMA_ENABLED", "1") not in ("0", "false", "False")

    from storage.event_store import EventStore
    store = EventStore("momentum")
    logger.info("Q&A bot online (chat %s, ollama=%s %s)", allowed, ollama_enabled, model)
    _send(token, allowed, HELP_TEXT)

    offset = None
    while True:
        try:
            url = f"{_API}/bot{token}/getUpdates?timeout=50"
            if offset is not None:
                url += f"&offset={offset}"
            with urllib.request.urlopen(url, timeout=60) as resp:
                updates = json.loads(resp.read().decode()).get("result", [])
        except Exception:  # noqa: BLE001
            time.sleep(3)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            msg = u.get("message") or u.get("edited_message") or {}
            chat_id = (msg.get("chat") or {}).get("id")
            text = (msg.get("text") or "").strip()
            if not text:
                continue
            if not is_authorized(chat_id, allowed):
                logger.warning("ignoring message from unauthorized chat %s", chat_id)
                continue
            try:
                reply = answer(store, text, host=host, model=model, timeout=timeout,
                               ollama_enabled=ollama_enabled)
            except Exception as exc:  # noqa: BLE001
                logger.warning("answer failed", exc_info=True)
                reply = f"(error answering: {exc})"
            _send(token, chat_id, reply)


if __name__ == "__main__":
    main()
