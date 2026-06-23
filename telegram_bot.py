#!/usr/bin/env python3
"""Standalone Telegram Q&A bot — answers free-form questions about the trading
data, READ-ONLY, locked to the authorized chat, Ollama-backed.

Runs as its OWN process (`python telegram_bot.py`) so it can NEVER impede the
trading loop. It long-polls getUpdates; for each message from TELEGRAM_CHAT_ID it
gathers a data snapshot from Postgres and asks the local Ollama model to answer.
If Ollama is down/disabled it falls back to the raw snapshot. It never accepts
trade commands — questions only (mirrors the view-only dashboard rule).

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (required), DATABASE_URL,
OLLAMA_HOST / OLLAMA_MODEL / OLLAMA_TIMEOUT_SECONDS / OLLAMA_ENABLED.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request

logger = logging.getLogger("telegram_bot")

_API = "https://api.telegram.org"
_ACTION_WORDS = ("buy", "sell", "flatten", "close all", "cancel", "short",
                 "liquidate", "exit all", "place order", "submit")


# ---------------------------------------------------------------- data context
def gather_context(store) -> str:
    """A compact, read-only snapshot of the current trading state for the LLM."""
    from storage.projections import (query_account_positions_snapshot,
                                      query_session_pnl, query_alltime_score)
    lines: list[str] = []
    try:
        p = query_session_pnl(store)
        lines.append(
            f"TODAY P&L: total {p.get('total_pnl', 0):+.0f} "
            f"(realized {p.get('realized_pnl', 0):+.0f}, "
            f"unrealized {p.get('unrealized_pnl', 0):+.0f}); "
            f"closed {p.get('closed_trades', 0)} "
            f"W/L {p.get('wins', 0)}/{p.get('losses', 0)}.")
        for t in (p.get("trades") or [])[:12]:
            lines.append(f"  closed {t.get('symbol')}: {t.get('pnl', t.get('realized_pnl', 0)):+.0f} "
                         f"({t.get('exit_reason', '')})")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"(P&L unavailable: {exc})")
    try:
        snap = query_account_positions_snapshot(store)
        positions = snap[0]["positions"] if snap else []
        if positions:
            lines.append(f"OPEN POSITIONS ({len(positions)}):")
            for q in positions:
                lines.append(
                    f"  {q.get('symbol')}: qty {q.get('quantity')} @ "
                    f"{q.get('avg_entry_price')} -> {q.get('current_price')} "
                    f"(uPL {q.get('unrealized_pnl', 0):+.0f})")
        else:
            lines.append("OPEN POSITIONS: none (flat).")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"(positions unavailable: {exc})")
    try:
        a = query_alltime_score(store)
        lines.append(f"ALL-TIME: realized {a.get('total_realized', 0):+.0f} over "
                     f"{a.get('trading_days', '?')} days, win% "
                     f"{(a.get('win_rate') or 0)*100:.0f}, trades {a.get('trades', '?')}.")
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(lines)


# ---------------------------------------------------------------------- ollama
def ask_ollama(question: str, context: str, *, host: str, model: str,
               timeout: int) -> str | None:
    """Free-form answer from the local model. None on any failure."""
    prompt = (
        "You are a concise, READ-ONLY assistant for a momentum day-trading bot. "
        "Answer the user's question using ONLY the data below. If the data does "
        "not contain the answer, say so. Do not invent numbers. Keep it short.\n\n"
        f"=== DATA ===\n{context}\n\n=== QUESTION ===\n{question}\n\n=== ANSWER ===\n")
    try:
        req = urllib.request.Request(
            f"{host}/api/generate",
            data=json.dumps({"model": model, "prompt": prompt, "stream": False,
                             "options": {"temperature": 0.2, "num_predict": 400}}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
        text = (body.get("response") or "").strip()
        return text or None
    except Exception:  # noqa: BLE001
        logger.debug("ollama unavailable", exc_info=True)
        return None


def answer(store, question: str, *, host: str, model: str, timeout: int,
           ollama_enabled: bool = True) -> str:
    """Build the read-only answer. Refuses action commands; falls back to the raw
    snapshot when Ollama is down/disabled (never crashes)."""
    if any(w in question.lower() for w in _ACTION_WORDS):
        return ("I'm read-only — I can answer questions about the data but I "
                "won't place or change trades. Ask me about P&L, positions, or "
                "today's trades.")
    context = gather_context(store)
    if ollama_enabled:
        out = ask_ollama(question, context, host=host, model=model, timeout=timeout)
        if out:
            return out
    return f"(LLM unavailable — raw snapshot)\n{context}"


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
    logger.info("Q&A bot online (chat %s, ollama=%s %s) — ask me about the data",
                allowed, ollama_enabled, model)
    _send(token, allowed, "\U0001F916 Q&A bot online — ask me about P&L, positions, or today's trades.")

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
