"""Telegram push notifier — gated alerts on MAJOR trading events.

Fully gated like the Ollama integration: if TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
are unset, every call is a SILENT no-op that NEVER raises into the trading loop.

Decoupled by design: it POLLS the event store for major events rather than
threading a notifier through the breaker / exit / execution subsystems, so it
cannot slow or break trading. One small scheduler step calls ``poll()``.

Major events (the only things worth a phone buzz):
  - daily-loss breaker trip          (risk_rule_triggered rule_type=daily_loss)
  - catastrophe-stop exit            (rule_type=exit_catastrophe)
  - naked-stop enforcement / flatten (rule_type=exit_naked_stop)
  - any closed trade with |realized| > BIG_TRADE_USD
  - a once-per-day EOD summary       (send_eod_summary, called by the loop)
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request

logger = logging.getLogger(__name__)

MAJOR_RULES = {"daily_loss", "exit_catastrophe", "exit_naked_stop"}
BIG_TRADE_USD = 300.0
_API = "https://api.telegram.org"


def telegram_enabled() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN")
                and os.environ.get("TELEGRAM_CHAT_ID"))


def send_telegram(text: str, *, token: str | None = None,
                  chat_id: str | None = None, timeout: int = 10) -> bool:
    """Send one message. Gated + fire-and-forget: returns False (never raises)
    if unconfigured or on any error — trading must never be impeded by Telegram."""
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    try:
        req = urllib.request.Request(
            f"{_API}/bot{token}/sendMessage",
            data=json.dumps({"chat_id": chat_id, "text": text,
                             "disable_web_page_preview": True}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return bool(json.loads(resp.read().decode()).get("ok"))
    except Exception:  # noqa: BLE001 — must never raise into the loop
        logger.debug("telegram send failed", exc_info=True)
        return False


def _fmt_rule(p: dict) -> str:
    rt = p.get("rule_type")
    icon = {"exit_catastrophe": "\U0001F6D1", "exit_naked_stop": "⚠️",
            "daily_loss": "\U0001F53B"}.get(rt, "❗")
    return f"{icon} {p.get('message') or rt}"


def _fmt_close(p: dict) -> str:
    pnl = p.get("realized_pnl") or 0.0
    icon = "\U0001F7E2" if pnl > 0 else "\U0001F534"
    return f"{icon} {p.get('symbol')} closed {pnl:+.0f} ({p.get('exit_reason') or ''})"


def fmt_eod(for_date: str, pnl: dict) -> str:
    total = pnl.get("total_pnl", 0.0)
    icon = "\U0001F4C8" if total >= 0 else "\U0001F4C9"
    wr = pnl.get("win_rate")
    wr_s = f"{wr*100:.0f}%" if wr is not None else "n/a"
    return (f"{icon} EOD {for_date}: {total:+.0f}\n"
            f"closed {pnl.get('closed_trades', '?')}  "
            f"W/L {pnl.get('wins', '?')}/{pnl.get('losses', '?')}  win% {wr_s}")


class TelegramNotifier:
    """Polls the event store and pushes one message per NEW major event since it
    started. In-memory dedup; primes 'seen' at startup so history is never
    replayed. All methods are no-ops + non-raising when Telegram is unconfigured."""

    def __init__(self, store, *, big_trade_usd: float = BIG_TRADE_USD):
        self.store = store
        self.big_trade_usd = big_trade_usd
        self._seen: set = set()
        self._eod_sent_date: str | None = None
        self.enabled = telegram_enabled()
        if self.enabled:
            try:
                self._scan(notify=False)  # prime: mark existing events as seen
            except Exception:  # noqa: BLE001
                logger.debug("notifier prime failed", exc_info=True)

    def poll(self) -> int:
        """One poll cycle. Returns messages sent. Never raises."""
        if not self.enabled:
            return 0
        try:
            return self._scan(notify=True)
        except Exception:  # noqa: BLE001
            logger.debug("notifier poll failed", exc_info=True)
            return 0

    def _scan(self, *, notify: bool) -> int:
        sent = 0
        for e in self.store.query_events(event_type="risk_rule_triggered", limit=200):
            p = json.loads(e["payload_json"])
            if p.get("rule_type") not in MAJOR_RULES:
                continue
            key = ("rule", p.get("rule_type"), str(e["timestamp"]), p.get("message"))
            if key in self._seen:
                continue
            self._seen.add(key)
            if notify and send_telegram(_fmt_rule(p)):
                sent += 1
        for e in self.store.query_events(event_type="position_closed", limit=200):
            p = json.loads(e["payload_json"])
            if abs(p.get("realized_pnl") or 0.0) < self.big_trade_usd:
                continue
            key = ("close", p.get("symbol"), str(e["timestamp"]))
            if key in self._seen:
                continue
            self._seen.add(key)
            if notify and send_telegram(_fmt_close(p)):
                sent += 1
        return sent

    def send_eod_summary(self, for_date: str, pnl: dict) -> bool:
        """One-per-day EOD summary. ``pnl`` = query_session_pnl(...) dict."""
        if not self.enabled or self._eod_sent_date == for_date:
            return False
        self._eod_sent_date = for_date
        return send_telegram(fmt_eod(for_date, pnl))
