"""Trading execution service tests: signal -> approval -> order -> exit."""

import json
from datetime import datetime, timedelta

import pytest

from alpaca_paper.execution import ExecutionRequest, ExecutionResult
from storage.event_schema import (
    AccountPositionsUpdatedEvent,
    EventMode,
    OrderFilledEvent,
    SignalReadyEvent,
)
from storage.event_store import EventStore
from storage.projections import query_approval_queue
from trading_execution import ExecutionSettings, TradingExecutionService
from trading_mode import TradingModeSettings

T0 = datetime(2026, 6, 11, 9, 45)


class FakeExecutor:
    broker_name = "alpaca_paper"

    def __init__(self, status: str = "accepted"):
        self.executed: list[ExecutionRequest] = []
        self.cancelled: list[dict] = []
        self.ok = True
        self.status = status  # "accepted" = resting/unfilled; "filled" = instant

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.executed.append(request)
        return ExecutionResult(
            ok=self.ok,
            order_id=request.order_id,
            broker_order_id="broker-1" if self.ok else None,
            status=self.status if self.ok else "error",
            error=None if self.ok else "broker down",
        )

    def cancel_entry(self, order_id, broker_order_id, symbol, reason):
        self.cancelled.append(
            {"order_id": order_id, "symbol": symbol, "reason": reason}
        )
        return ExecutionResult(ok=True, order_id=order_id, status="cancelled")


@pytest.fixture
def store():
    s = EventStore(":memory:")
    yield s
    s.close()


class Clock:
    """A controllable clock for deterministic wall-clock timeout tests."""

    def __init__(self, start: datetime | None = None):
        self.t = start or datetime(2026, 6, 11, 9, 45)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, minutes: float) -> None:
        self.t += timedelta(minutes=minutes)


def emit_signal(store, symbol="GOOD", entry=14.0, stop=13.45,
                above_vwap=None, vwap=None, day_open=None, cum_volume=None, quality_score=0.7):
    signal_data = {
        "entry_price": entry,
        "stop_loss_price": stop,
        "quality_score": quality_score,
    }
    if above_vwap is not None:
        signal_data["above_vwap"] = above_vwap
        signal_data["vwap"] = vwap if vwap is not None else (
            round(entry - 1.0, 2) if above_vwap else round(entry + 1.0, 2))
    if day_open is not None:
        signal_data["day_open"] = day_open
    if cum_volume is not None:
        signal_data["cum_volume"] = cum_volume
    store.emit(
        SignalReadyEvent(
            timestamp=T0,
            mode=EventMode.PAPER,
            correlation_id="t",
            message=f"{symbol} ready",
            symbol=symbol,
            signal_type="bull_flag",
            confidence=0.9,
            signal_data=signal_data,
        )
    )


def emit_positions(store, positions):
    store.emit(
        AccountPositionsUpdatedEvent(
            timestamp=T0,
            mode=EventMode.PAPER,
            message="pos",
            broker_name="alpaca_paper",
            account_id="paper",
            positions=positions,
        )
    )


def make_service(store, executor=None, price_provider=None, now_fn=None, **settings_kw):
    defaults = dict(
        enabled=True, auto_approve=False, max_orders_per_tick=2,
        max_concurrent_positions=3, risk_per_trade_pct=0.01,
        default_equity=100_000.0,
    )
    defaults.update(settings_kw)
    return TradingExecutionService(
        store,
        executor=executor or FakeExecutor(),
        settings=ExecutionSettings(**defaults),
        trading_mode=TradingModeSettings(execution_mode="alpaca_paper"),
        session_id="t",
        price_provider=price_provider,
        now_fn=now_fn,
    )


def test_ready_signal_creates_approval_request(store):
    emit_signal(store)
    service = make_service(store)
    requested = service.request_approvals_for_ready_signals()
    assert len(requested) == 1  # returns order ids
    queue = query_approval_queue(store)
    assert len(queue) == 1
    request = queue[0]["execution_request"]
    # risk sizing: 1% of 100k = $1000 risk / $0.55 per share ≈ 1818 shares
    assert request["quantity"] == int(1000 / 0.55)
    assert request["stop_loss_price"] == 13.45
    # take profit at entry + 2R
    assert request["take_profit_price"] == pytest.approx(14.0 + 2 * 0.55)


def test_manual_approval_executes_order(store):
    emit_signal(store)
    executor = FakeExecutor()
    service = make_service(store, executor)
    service.request_approvals_for_ready_signals()
    order_id = query_approval_queue(store)[0]["order_id"]

    result = service.approve_order(order_id, approved_by="dashboard")
    assert result["ok"]
    assert executor.executed[0].symbol == "GOOD"
    assert store.query_events(event_type="order_approved")
    assert query_approval_queue(store) == []  # no longer pending


def test_rejection_clears_queue_without_executing(store):
    emit_signal(store)
    executor = FakeExecutor()
    service = make_service(store, executor)
    service.request_approvals_for_ready_signals()
    order_id = query_approval_queue(store)[0]["order_id"]

    result = service.reject_order(order_id, reason="too extended")
    assert result["ok"]
    assert executor.executed == []
    assert query_approval_queue(store) == []
    assert store.query_events(event_type="order_rejected")


def test_approving_unknown_order_fails_cleanly(store):
    service = make_service(store)
    assert not service.approve_order("nope")["ok"]
    assert not service.reject_order("nope")["ok"]


def test_auto_approve_tick_executes(store):
    emit_signal(store)
    executor = FakeExecutor()
    service = make_service(store, executor, auto_approve=True)
    out = service.tick()
    assert len(out["approvals_requested"]) == 1
    assert len(out["auto_executed"]) == 1
    assert executor.executed[0].symbol == "GOOD"


def test_daily_entry_cap_enforced_on_auto_path(store):
    # Regression: live entries flow through the AUTO approval path, not the fast
    # submit_breakout_now path, so the per-day cap must bind HERE. cap=2 over 3
    # distinct signals => exactly 2 execute; the 3rd is rejected (not left pending).
    for sym, e, s in [("AAA", 14.0, 13.45), ("BBB", 8.0, 7.60), ("CCC", 5.0, 4.70)]:
        emit_signal(store, symbol=sym, entry=e, stop=s)
    executor = FakeExecutor()
    service = make_service(store, executor, auto_approve=True,
                           max_fresh_entries_per_day=2,
                           max_concurrent_positions=10, max_orders_per_tick=10)
    service.request_approvals_for_ready_signals()
    ids = [q["order_id"] for q in query_approval_queue(store)]
    [service.approve_order(oid, approved_by="auto") for oid in ids]
    assert len(executor.executed) == 2                   # cap binds at 2
    assert query_approval_queue(store) == []             # capped order rejected, not left pending
    rejects = store.query_events(event_type="order_rejected")
    assert len(rejects) == 1                              # exactly the 3rd
    assert json.loads(rejects[0]["payload_json"])["rejection_reason"] == "daily_entry_cap"


def test_cap_rejection_distinguishable_from_execution(store):
    # Log-visibility fix: a cap-rejected auto approval must be tagged rejected=True with
    # the reason and carry NO broker_order_id, so the loop log counts it as auto_rejected
    # instead of masquerading as auto_executed=1 with no matching broker order.
    for sym, e, s in [("AAA", 14.0, 13.45), ("BBB", 8.0, 7.60), ("CCC", 5.0, 4.70)]:
        emit_signal(store, symbol=sym, entry=e, stop=s)
    service = make_service(store, FakeExecutor(), auto_approve=True,
                           max_fresh_entries_per_day=2,
                           max_concurrent_positions=10, max_orders_per_tick=10)
    service.request_approvals_for_ready_signals()
    ids = [q["order_id"] for q in query_approval_queue(store)]
    results = [service.approve_order(oid, approved_by="auto") for oid in ids]
    # the exact split run_live_paper.step_execute() uses for the log line
    executed = [r for r in results if r.get("broker_order_id")]
    rejected = [r for r in results if r.get("rejected")]
    assert len(executed) == 2 and len(rejected) == 1     # 2 real submissions, 1 cap-reject
    assert all(not r.get("rejected") for r in executed)
    assert rejected[0]["reason"] == "daily_entry_cap"
    assert "broker_order_id" not in rejected[0]          # no phantom broker order counted


def test_daily_cap_never_blocks_manual_approval(store):
    # A human override (approved_by="dashboard") is never capped.
    for sym, e, s in [("AAA", 14.0, 13.45), ("BBB", 8.0, 7.60)]:
        emit_signal(store, symbol=sym, entry=e, stop=s)
    executor = FakeExecutor()
    service = make_service(store, executor, max_fresh_entries_per_day=1,
                           max_concurrent_positions=10, max_orders_per_tick=10)
    service.request_approvals_for_ready_signals()
    ids = [q["order_id"] for q in query_approval_queue(store)]
    for oid in ids:
        assert service.approve_order(oid, approved_by="dashboard")["ok"]
    assert len(executor.executed) == 2                   # cap ignored for manual override


def test_vwap_gate_skips_below_vwap_when_enforced(store):
    emit_signal(store, symbol="LOWV", entry=14.0, stop=13.45, above_vwap=False, vwap=15.0)
    service = make_service(store, require_above_vwap=True)
    created = service.request_approvals_for_ready_signals()
    assert created == []                                  # entry skipped
    assert query_approval_queue(store) == []
    vb = [json.loads(e["payload_json"]) for e in store.query_events(event_type="risk_rule_triggered")
          if json.loads(e["payload_json"])["rule_type"] == "vwap_below"]
    assert len(vb) == 1 and vb[0]["action_taken"] == "skipped_entry"


def test_vwap_gate_shadow_logs_but_allows_when_off(store):
    emit_signal(store, symbol="LOWV", entry=14.0, stop=13.45, above_vwap=False, vwap=15.0)
    service = make_service(store, require_above_vwap=False)
    created = service.request_approvals_for_ready_signals()
    assert len(created) == 1                              # still entered (shadow mode)
    vb = [json.loads(e["payload_json"]) for e in store.query_events(event_type="risk_rule_triggered")
          if json.loads(e["payload_json"])["rule_type"] == "vwap_below"]
    assert len(vb) == 1 and vb[0]["action_taken"] == "shadow_logged"


def test_quality_gate_skips_low_grade_when_enforced(store):
    emit_signal(store, symbol="CHOP", quality_score=0.30)     # F-grade chop
    service = make_service(store, min_quality_score=0.50)
    created = service.request_approvals_for_ready_signals()
    assert created == []                                      # entry skipped
    assert query_approval_queue(store) == []
    qb = [json.loads(e["payload_json"]) for e in store.query_events(event_type="risk_rule_triggered")
          if json.loads(e["payload_json"])["rule_type"] == "quality_below"]
    assert len(qb) == 1 and qb[0]["action_taken"] == "skipped_entry"


def test_quality_gate_off_allows_low_grade(store):
    emit_signal(store, symbol="CHOP", quality_score=0.30)
    created = make_service(store, min_quality_score=0.0).request_approvals_for_ready_signals()
    assert len(created) == 1                                  # gate off -> still enters


def test_quality_gate_allows_at_or_above_threshold(store):
    emit_signal(store, symbol="GOOD", quality_score=0.72)     # B-grade
    created = make_service(store, min_quality_score=0.50).request_approvals_for_ready_signals()
    assert len(created) == 1


def test_vwap_gate_allows_above_vwap(store):
    emit_signal(store, symbol="HIV", entry=14.0, stop=13.45, above_vwap=True, vwap=13.0)
    service = make_service(store, require_above_vwap=True)
    created = service.request_approvals_for_ready_signals()
    assert len(created) == 1
    assert not [e for e in store.query_events(event_type="risk_rule_triggered")
                if json.loads(e["payload_json"])["rule_type"] == "vwap_below"]


def test_vwap_gate_fail_open_on_missing_field(store):
    # signals lacking the above_vwap field (old signals) must NEVER be blocked
    emit_signal(store, symbol="OLDV", entry=14.0, stop=13.45)  # no above_vwap
    service = make_service(store, require_above_vwap=True)
    assert len(service.request_approvals_for_ready_signals()) == 1


def test_anti_chase_skip_logic(store):
    svc = make_service(store)   # defaults: ext 0.15, day-ext 0.30, halt-guard on
    assert svc._anti_chase_skip(12.0, 10.0, None, False) == "over_extended"        # +20% > 15%
    assert svc._anti_chase_skip(14.0, 14.0, 10.0, False) == "over_extended_day"    # +40% > 30%
    assert svc._anti_chase_skip(10.0, 10.0, 10.0, True) == "halted_symbol"
    assert svc._anti_chase_skip(10.0, 10.0, 9.0, False) is None                    # clean
    assert svc._anti_chase_skip(14.0, 14.0, None, False) is None                   # fail-open day_open


def test_unified_entry_blocks_parabolic_day_on_auto_path(store):
    # entry 14 is +40% above the day open 10 -> day-extension gate blocks it
    emit_signal(store, symbol="PARA", entry=14.0, stop=13.45, above_vwap=True, day_open=10.0)
    created = make_service(store, unified_entry=True).request_approvals_for_ready_signals()
    assert created == []
    rr = [json.loads(e["payload_json"]) for e in store.query_events(event_type="risk_rule_triggered")
          if json.loads(e["payload_json"])["rule_type"] == "over_extended_day"]
    assert len(rr) == 1 and rr[0]["action_taken"] == "skipped_entry"


def test_unified_entry_off_allows_parabolic_day(store):
    emit_signal(store, symbol="PARA", entry=14.0, stop=13.45, above_vwap=True, day_open=10.0)
    assert len(make_service(store, unified_entry=False).request_approvals_for_ready_signals()) == 1


def test_unified_entry_fail_open_missing_day_open(store):
    emit_signal(store, symbol="NODO", entry=14.0, stop=13.45, above_vwap=True)  # no day_open
    assert len(make_service(store, unified_entry=True).request_approvals_for_ready_signals()) == 1


def test_unified_entry_allows_normal_day(store):
    # entry 14 is +7.7% above day open 13 -> under the 30% ceiling -> allowed
    emit_signal(store, symbol="OKDAY", entry=14.0, stop=13.45, above_vwap=True, day_open=13.0)
    assert len(make_service(store, unified_entry=True).request_approvals_for_ready_signals()) == 1


def test_liquidity_cap_sizes_down_thin_name_on_auto_path(store):
    # thin name (cum_volume 10k) -> shares capped to liq*cum_volume, not risk-sized
    emit_signal(store, symbol="THIN", entry=2.0, stop=1.9, above_vwap=True,
                day_open=1.95, cum_volume=10_000)
    svc = make_service(store, unified_entry=True, liquidity_max_volume_pct=0.01,
                       max_concurrent_positions=10)
    svc.request_approvals_for_ready_signals()
    q = query_approval_queue(store)[0]["execution_request"]["quantity"]
    assert q <= int(0.01 * 10_000)            # liquidity-capped at 100 shares


def test_liquidity_cap_off_when_unified_disabled(store):
    emit_signal(store, symbol="THIN", entry=2.0, stop=1.9, above_vwap=True,
                day_open=1.95, cum_volume=10_000)
    svc = make_service(store, unified_entry=False, liquidity_max_volume_pct=0.01,
                       max_concurrent_positions=10)
    svc.request_approvals_for_ready_signals()
    q = query_approval_queue(store)[0]["execution_request"]["quantity"]
    assert q > int(0.01 * 10_000)             # not capped -> risk-based size is larger


def test_fill_model_resting_rests_at_level(store):
    emit_signal(store, symbol="REST", entry=14.0, stop=13.45, above_vwap=True)
    make_service(store).request_approvals_for_ready_signals()   # default resting
    req = query_approval_queue(store)[0]["execution_request"]
    assert req["entry_price"] == 14.0                            # rests AT the level


def test_fill_model_marketable_lifts_above_trigger(store):
    emit_signal(store, symbol="MKT", entry=14.0, stop=13.45, above_vwap=True)
    make_service(store, entry_fill_model="marketable").request_approvals_for_ready_signals()
    req = query_approval_queue(store)[0]["execution_request"]
    assert req["entry_price"] == round(14.0 * 1.004, 2)         # a hair above (slippage 0.4%)
    assert req["entry_price"] > 14.0
    assert req["stop_loss_price"] == 13.45                      # R still measured from the level


def test_no_duplicate_requests_for_same_symbol(store):
    emit_signal(store)
    service = make_service(store)
    assert len(service.request_approvals_for_ready_signals()) == 1
    assert service.request_approvals_for_ready_signals() == []
    assert len(query_approval_queue(store)) == 1


def test_held_symbols_are_skipped(store):
    emit_signal(store)
    emit_positions(store, [{"symbol": "GOOD", "quantity": 10}])
    service = make_service(store)
    assert service.request_approvals_for_ready_signals() == []


def test_max_concurrent_positions_guard(store):
    emit_signal(store, symbol="NEWS")
    emit_positions(
        store,
        [{"symbol": s, "quantity": 1} for s in ("AAA", "BBB", "CCC")],
    )
    service = make_service(store, max_concurrent_positions=3)
    assert service.request_approvals_for_ready_signals() == []
    assert store.query_events(event_type="risk_rule_triggered")


def test_exit_order_closes_position(store):
    emit_positions(store, [{"symbol": "GOOD", "quantity": 10}])
    executor = FakeExecutor()
    service = make_service(store, executor)
    result = service.submit_exit_order("GOOD")
    assert result["ok"]
    exit_request = executor.executed[0]
    assert exit_request.side == "sell"
    assert exit_request.quantity == 10


def test_exit_order_without_position_fails(store):
    service = make_service(store)
    result = service.submit_exit_order("NOPE")
    assert not result["ok"]


# ---------------------------------------------------------------------------
# Auto-arm entry mechanism: tunable reward, entry order type, and back-out
# ---------------------------------------------------------------------------

def test_reward_multiple_is_configurable(store):
    emit_signal(store)  # entry 14.0 stop 13.45 -> risk 0.55
    service = make_service(store, reward_multiple=3.0)
    service.request_approvals_for_ready_signals()
    req = query_approval_queue(store)[0]["execution_request"]
    assert req["take_profit_price"] == pytest.approx(14.0 + 3.0 * 0.55)


def test_entry_order_type_flows_into_request(store):
    emit_signal(store)
    service = make_service(store, entry_order_type="limit")
    service.request_approvals_for_ready_signals()
    req = query_approval_queue(store)[0]["execution_request"]
    assert req["order_type"] == "limit"


def test_unfilled_entry_is_armed_then_times_out(store):
    emit_signal(store)
    executor = FakeExecutor(status="accepted")  # resting limit, never fills
    clock = Clock()
    service = make_service(
        store, executor, auto_approve=True, now_fn=clock,
        entry_timeout_bars=2, entry_invalidate_pct=-1.0,  # disable price-break
    )
    # request + auto-approve -> order is armed, not yet expired
    service.tick()
    assert len(service._armed) == 1
    assert executor.cancelled == []
    # 1 minute later: under the 2-minute window -> still armed
    clock.advance(1)
    assert service.expire_stale_entries() == []
    assert len(service._armed) == 1
    # 2 minutes after arming -> timed out and backed out
    clock.advance(1)
    out = service.expire_stale_entries()
    assert len(out) == 1
    assert len(executor.cancelled) == 1
    assert "timed out" in executor.cancelled[0]["reason"]
    assert service._armed == {}


def test_late_confirmation_skips_signal(store):
    # Confirmation-speed gate: the signal went ready at T0=09:45 (minute 15 of the
    # session); with the gate at 10 minutes it must be SKIPPED and shadow-logged,
    # never executed. This is the backtested +EV filter (early confirms +0.42R,
    # late confirms dead).
    emit_signal(store)                                   # ready at T0 = minute 15
    executor = FakeExecutor()
    service = make_service(store, executor, auto_approve=True,
                           entry_confirm_by_minute=10)
    service.tick()
    assert executor.executed == []                       # never reached the broker
    rejects = store.query_events(event_type="risk_rule_triggered")
    assert any(json.loads(r["payload_json"]).get("rule_type") == "late_confirmation"
               for r in rejects)


def test_early_confirmation_passes(store):
    # same signal, gate at 20 minutes -> minute-15 confirmation is fine
    emit_signal(store)
    executor = FakeExecutor()
    service = make_service(store, executor, auto_approve=True,
                           entry_confirm_by_minute=20)
    service.tick()
    assert len(executor.executed) == 1


def test_confirmation_gate_off_by_default(store):
    emit_signal(store)
    executor = FakeExecutor()
    service = make_service(store, executor, auto_approve=True)   # gate default 0 = off
    service.tick()
    assert len(executor.executed) == 1


def test_fast_path_blocks_late_breakout(store):
    # the live trigger path uses now() as the confirmation time
    executor = FakeExecutor()
    clock = Clock(datetime(2026, 6, 11, 11, 0))          # 11:00 = minute 90
    service = make_service(store, executor, now_fn=clock, entry_confirm_by_minute=15)
    out = service.submit_breakout_now("GOOD", trigger=14.0, stop=13.45, last_price=14.01)
    assert out.get("skipped") == "late_confirmation"
    clock.t = datetime(2026, 6, 11, 9, 40)               # minute 10 -> inside the window
    out2 = service.submit_breakout_now("GOOD", trigger=14.0, stop=13.45, last_price=14.01)
    assert out2.get("skipped") != "late_confirmation"


def test_backed_out_unfilled_entry_frees_its_cap_slot(store):
    # Behavior fix: the daily cap counts FILLS, not submissions. Two resting entries consume
    # the cap, but when they back out UNFILLED their slots are freed (the 2026-07-01/02 bug
    # where phantom unfilled limits capped the day after only a few real trades).
    for sym, e, s in [("AAA", 14.0, 13.45), ("BBB", 8.0, 7.60), ("CCC", 5.0, 4.70)]:
        emit_signal(store, symbol=sym, entry=e, stop=s)
    executor = FakeExecutor(status="accepted")   # resting limits, never fill
    clock = Clock()
    service = make_service(store, executor, auto_approve=True, now_fn=clock,
                           max_fresh_entries_per_day=2, max_concurrent_positions=10,
                           max_orders_per_tick=10, entry_timeout_bars=2, entry_invalidate_pct=-1.0)
    service.request_approvals_for_ready_signals()
    ids = [q["order_id"] for q in query_approval_queue(store)]
    results = [service.approve_order(oid, approved_by="auto") for oid in ids]
    assert service._fresh_entries["n"] == 2                     # 2 armed+counted, cap hit
    assert sum(1 for r in results if r.get("rejected")) == 1    # 3rd was cap-rejected
    assert len(service._armed) == 2
    clock.advance(3)                                            # both time out unfilled
    service.expire_stale_entries()
    assert service._armed == {}
    assert service._fresh_entries["n"] == 0                     # <-- the fix: slots freed


def test_fast_guard_does_not_shorten_timeout(store):
    """Checking many times a second must not trip the wall-clock timeout early."""
    emit_signal(store)
    executor = FakeExecutor(status="accepted")
    clock = Clock()
    service = make_service(
        store, executor, auto_approve=True, now_fn=clock,
        entry_timeout_bars=2, entry_invalidate_pct=-1.0,
    )
    service.tick()
    # hammer the guard 50 times with no time passing -> no premature back-out
    for _ in range(50):
        assert service.expire_stale_entries() == []
    assert len(service._armed) == 1
    assert executor.cancelled == []


def test_unfilled_entry_invalidated_by_price_break(store):
    emit_signal(store, entry=14.0, stop=13.45)
    executor = FakeExecutor(status="accepted")
    price = {"GOOD": 14.10}  # starts above the trigger
    service = make_service(
        store, executor, auto_approve=True,
        entry_timeout_bars=0,  # disable timeout; isolate price-break
        entry_invalidate_pct=0.0,  # any trade below entry invalidates
        price_provider=lambda sym: price.get(sym),
    )
    service.tick()
    assert len(service._armed) == 1
    # backdate past the fill-grace window so the price-break logic can act
    from datetime import datetime, timedelta
    for a in service._armed.values():
        a["armed_at"] = datetime.now() - timedelta(seconds=30)
    # price holds above entry -> no back-out
    assert service.expire_stale_entries() == []
    # price breaks back below the entry trigger -> cancel
    price["GOOD"] = 13.90
    out = service.expire_stale_entries()
    assert len(out) == 1
    assert "invalidated" in executor.cancelled[0]["reason"]
    assert service._armed == {}


def test_armed_entry_cleared_on_fill_without_cancel(store):
    emit_signal(store)
    executor = FakeExecutor(status="accepted")
    service = make_service(
        store, executor, auto_approve=True, entry_timeout_bars=1,
    )
    service.tick()
    assert len(service._armed) == 1
    order_id = next(iter(service._armed))
    # broker reports the fill via the event store
    store.emit(
        OrderFilledEvent(
            timestamp=T0, mode=EventMode.PAPER, message="fill",
            order_id=order_id, symbol="GOOD", fill_price=14.01, fill_quantity=10,
        )
    )
    out = service.expire_stale_entries()
    # filled -> tracking stops, nothing cancelled even though timeout=1
    assert out == []
    assert executor.cancelled == []
    assert service._armed == {}


def test_backout_frees_symbol_to_resignal(store):
    emit_signal(store)
    executor = FakeExecutor(status="accepted")
    clock = Clock()
    service = make_service(
        store, executor, auto_approve=True, now_fn=clock,
        entry_timeout_bars=1, entry_invalidate_pct=-1.0,
    )
    service.tick()
    symbol = service._armed[next(iter(service._armed))]["symbol"]
    assert symbol in service._requested_symbols
    clock.advance(1)  # reach the 1-minute timeout
    service.expire_stale_entries()
    # after backing out, the symbol is no longer marked as requested
    assert symbol not in service._requested_symbols


# ---------------------------------------------------------------------------
# Backout cooldown (anti-thrash): a name that won't fill is benched, not
# re-armed every pass (NEOV armed+backed-out 68x in one live session).
# ---------------------------------------------------------------------------

def test_backout_benches_symbol_until_cooldown_elapses(store):
    emit_signal(store)
    executor = FakeExecutor(status="accepted")
    clock = Clock()
    service = make_service(
        store, executor, auto_approve=True, now_fn=clock,
        entry_timeout_bars=1, entry_invalidate_pct=-1.0,
        backout_cooldown_seconds=120, max_backouts_per_symbol=3,
    )
    service.tick()
    sym = service._armed[next(iter(service._armed))]["symbol"]
    clock.advance(1)  # hit the timeout -> backs out
    out = service.expire_stale_entries()
    assert len(out) == 1 and "cooldown" in out[0]
    # benched right after the backout; a fresh ready signal must NOT re-arm it
    assert service._in_cooldown(sym)
    emit_signal(store)
    assert service.request_approvals_for_ready_signals() == []
    # still benched just before the window closes...
    clock.advance(1.9)  # 114s elapsed (< 120)
    assert service._in_cooldown(sym)
    # ...and eligible once the cooldown elapses
    clock.advance(0.2)  # 126s elapsed (> 120)
    assert not service._in_cooldown(sym)
    assert len(service.request_approvals_for_ready_signals()) == 1


def test_reentry_blocked_after_position_closes(store):
    """Once a name's position closes (stop-out / trail), it must not be re-entered
    this session — both the fast trigger and the slow approval path skip it."""
    from alpaca_paper.execution import AlpacaPaperExecutor

    class _Cli:
        def __init__(self):
            self.pos = []

        def get_positions(self, fresh=False):
            return list(self.pos)

        def get_account(self):
            return {"equity": "100000", "last_equity": "100000"}

        def submit_order(self, **kw):
            return {"id": "o", "status": "new", "filled_qty": "0"}

        def cancel_order(self, order_id):
            pass

    cli = _Cli()
    svc = TradingExecutionService(
        store, executor=AlpacaPaperExecutor(store, client=cli),
        settings=ExecutionSettings(reentry_block_after_exit=True, auto_approve=True,
                                   reentry_min_loss_pct=0.01, max_daily_loss_pct=0.5),
        session_id="t",
    )
    cli.pos = [{"symbol": "AAA", "qty": "100", "unrealized_plpc": "-0.05"}]  # down 5%
    svc.expire_stale_entries()                 # records AAA as held (losing)
    assert "AAA" not in svc._exited_today
    cli.pos = []                               # AAA stops out
    svc.expire_stale_entries()                 # detects the losing departure
    assert "AAA" in svc._exited_today
    # fast path blocked
    res = svc.submit_breakout_now("AAA", trigger=10.0, stop=9.5, last_price=10.0)
    assert res["ok"] is False and res["skipped"] == "reentry_blocked"
    # slow path blocked too
    emit_signal(store, symbol="AAA", entry=10.0, stop=9.5)
    assert svc.request_approvals_for_ready_signals() == []


def test_reentry_allowed_after_winning_exit(store):
    """A name that scratched or WON is not benched — a quick re-entry can be the
    right call (the GRAB +513 case the blunt guard would have killed)."""
    from alpaca_paper.execution import AlpacaPaperExecutor

    class _Cli:
        def __init__(self):
            self.pos = []

        def get_positions(self, fresh=False):
            return list(self.pos)

        def get_account(self):
            return {"equity": "100000", "last_equity": "100000"}

        def submit_order(self, **kw):
            return {"id": "o", "status": "new", "filled_qty": "0"}

        def cancel_order(self, order_id):
            pass

    cli = _Cli()
    svc = TradingExecutionService(
        store, executor=AlpacaPaperExecutor(store, client=cli),
        settings=ExecutionSettings(reentry_block_after_exit=True, auto_approve=True,
                                   reentry_min_loss_pct=0.01, max_daily_loss_pct=0.5),
        session_id="t",
    )
    cli.pos = [{"symbol": "WIN", "qty": "100", "unrealized_plpc": "0.04"}]  # up 4%
    svc.expire_stale_entries()
    cli.pos = []                               # WIN exits in profit
    svc.expire_stale_entries()
    assert "WIN" not in svc._exited_today       # not benched -> re-entry allowed
    res = svc.submit_breakout_now("WIN", trigger=10.0, stop=9.5, last_price=10.0)
    assert res["ok"] is True


def test_repeated_backouts_bench_symbol_for_session(store):
    clock = Clock()
    service = make_service(
        store, now_fn=clock,
        backout_cooldown_seconds=60, max_backouts_per_symbol=3,
    )
    # first two backouts only cool the name temporarily
    for _ in range(2):
        service._register_backout("THRASH")
        assert service._in_cooldown("THRASH")
        clock.advance(2)  # 120s > 60s -> cooldown elapses
        assert not service._in_cooldown("THRASH")
    # the third hits the cap -> benched for the rest of the session
    service._register_backout("THRASH")
    clock.advance(10_000)  # hours later, still benched
    assert service._in_cooldown("THRASH")


# --- catalyst dilution veto (Phase 2; gated OFF by default) -----------------

def _service_with_catalyst(store, advisory, *, veto_enabled, executor=None):
    settings = ExecutionSettings(
        enabled=True, auto_approve=False, max_orders_per_tick=2,
        max_concurrent_positions=3, risk_per_trade_pct=0.01,
        default_equity=100_000.0,
        catalyst_veto_enabled=veto_enabled,
        catalyst_veto_min_conviction=0.6,
    )
    return TradingExecutionService(
        store,
        executor=executor or FakeExecutor(),
        settings=settings,
        trading_mode=TradingModeSettings(execution_mode="alpaca_paper"),
        session_id="t",
        catalyst_provider=lambda sym: advisory,
    )


def test_dilution_veto_blocks_entry_and_emits_event(store):
    emit_signal(store, symbol="DILUT")
    advisory = {"is_dilutive": True, "conviction": 0.9,
                "catalyst_type": "offering_dilution", "sentiment": -0.8,
                "rationale": "registered direct offering"}
    service = _service_with_catalyst(store, advisory, veto_enabled=True)

    requested = service.request_approvals_for_ready_signals()
    assert requested == []                       # no approval created
    assert query_approval_queue(store) == []
    risk_events = store.query_events(event_type="risk_rule_triggered")
    assert any(
        __import__("json").loads(e["payload_json"]).get("rule_type")
        == "catalyst_dilution_veto"
        for e in risk_events
    )


def test_dilution_veto_disabled_allows_entry(store):
    emit_signal(store, symbol="DILUT")
    advisory = {"is_dilutive": True, "conviction": 0.9}
    service = _service_with_catalyst(store, advisory, veto_enabled=False)
    assert len(service.request_approvals_for_ready_signals()) == 1


def test_dilution_veto_below_conviction_allows_entry(store):
    emit_signal(store, symbol="MAYBE")
    advisory = {"is_dilutive": True, "conviction": 0.4}  # under the 0.6 floor
    service = _service_with_catalyst(store, advisory, veto_enabled=True)
    assert len(service.request_approvals_for_ready_signals()) == 1


def test_non_dilutive_catalyst_allows_entry(store):
    emit_signal(store, symbol="FDA")
    advisory = {"is_dilutive": False, "conviction": 0.95,
                "catalyst_type": "fda_approval"}
    service = _service_with_catalyst(store, advisory, veto_enabled=True)
    assert len(service.request_approvals_for_ready_signals()) == 1


def test_breakout_now_respects_dilution_veto(store):
    advisory = {"is_dilutive": True, "conviction": 0.9}
    service = _service_with_catalyst(store, advisory, veto_enabled=True)
    res = service.submit_breakout_now("DILUT", trigger=14.0, stop=13.45, last_price=14.0)
    assert res["ok"] is False and res["skipped"] == "dilution_veto"
