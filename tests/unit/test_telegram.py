"""Telegram notifier (push) + Q&A bot (interactive) — gating, dedup, auth, fallback."""

import pytest

import runtime.notifier as nf
import telegram_bot as qa
from storage.event_schema import EventMode, PositionClosedEvent, RiskRuleTriggeredEvent
from storage.event_store import EventStore


@pytest.fixture
def store():
    s = EventStore(":memory:")
    yield s
    s.close()


def _rule(store, rule_type, message="m"):
    store.emit(RiskRuleTriggeredEvent(
        timestamp=__import__("datetime").datetime(2026, 6, 23, 10, 0),
        mode=EventMode.PAPER, correlation_id="t", message=message,
        rule_type=rule_type, rule_value=0.0, current_state={}, action_taken="x"))


def _close(store, symbol, pnl):
    store.emit(PositionClosedEvent(
        timestamp=__import__("datetime").datetime(2026, 6, 23, 10, 1),
        mode=EventMode.PAPER, correlation_id="t", message="c",
        position_id=f"{symbol}-1", symbol=symbol, exit_price=10.0,
        realized_pnl=pnl, exit_reason="stop_loss"))


# ----------------------------------------------------------------- C1 notifier
def test_send_telegram_noop_when_unconfigured(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert nf.send_telegram("hi") is False          # silent no-op, no network
    assert nf.telegram_enabled() is False


def test_notifier_disabled_is_inert(store, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    n = nf.TelegramNotifier(store)
    _rule(store, "daily_loss")
    assert n.poll() == 0                              # never sends, never raises


def test_notifier_pushes_major_events_once(store, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    sent = []
    monkeypatch.setattr(nf, "send_telegram", lambda text, **kw: sent.append(text) or True)
    n = nf.TelegramNotifier(store)                    # primes on empty store
    _rule(store, "exit_catastrophe", "CAT")           # major
    _rule(store, "entry_backout", "noise")            # routine -> ignored
    _close(store, "BIG", 553)                          # > $300 -> major
    _close(store, "SMALL", 40)                         # < $300 -> ignored
    assert n.poll() == 2
    assert n.poll() == 0                              # dedup: no re-send
    joined = " ".join(sent)
    assert "CAT" in joined and "BIG" in joined
    assert "noise" not in joined and "SMALL" not in joined


def test_eod_summary_once_per_day(store, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    sent = []
    monkeypatch.setattr(nf, "send_telegram", lambda text, **kw: sent.append(text) or True)
    n = nf.TelegramNotifier(store)
    pnl = {"total_pnl": -2464, "closed_trades": 12, "wins": 3, "losses": 9, "win_rate": 0.25}
    assert n.send_eod_summary("2026-06-23", pnl) is True
    assert n.send_eod_summary("2026-06-23", pnl) is False   # not twice
    assert "2026-06-23" in sent[0] and "-2464" in sent[0]


# ------------------------------------------------------------------- C2 Q&A bot
def test_auth_gate():
    assert qa.is_authorized(2082795871, "2082795871") is True
    assert qa.is_authorized(999, "2082795871") is False
    assert qa.is_authorized(2082795871, None) is False


def test_answer_refuses_action_commands(store):
    for cmd in ("buy 100 AAPL", "flatten everything", "sell PLUG now"):
        out = qa.answer(store, cmd, host="h", model="m", timeout=1, ollama_enabled=True)
        assert "read-only" in out.lower()


def test_answer_falls_back_when_ollama_disabled(store):
    out = qa.answer(store, "what's my P&L?", host="h", model="m", timeout=1,
                    ollama_enabled=False)
    assert "raw snapshot" in out.lower()
    assert "TODAY P&L" in out                          # gather_context ran


def test_answer_falls_back_when_ollama_down(store, monkeypatch):
    monkeypatch.setattr(qa, "ask_ollama", lambda *a, **k: None)   # simulate down
    out = qa.answer(store, "how are positions?", host="h", model="m", timeout=1,
                    ollama_enabled=True)
    assert "raw snapshot" in out.lower()
