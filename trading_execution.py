"""Trading execution service (Milestone 4/5 glue).

Connects ready signals to broker execution with risk controls and an
approval workflow:

    signal_ready
      -> risk checks (sizing, max concurrent, daily-loss circuit breaker)
      -> order_approval_requested
      -> [manual approval in the dashboard]  or  [auto-approve]
      -> order_approved -> broker submit -> order_submitted (+ filled)

Exit orders close existing broker positions on demand.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

from runtime.flatten import (buy_fills_from_orders, cancel_protective_and_close,
                             find_overnight_carries)
from alpaca_paper.execution import (
    AlpacaPaperExecutor,
    ExecutionRequest,
)
from storage.event_schema import (
    EventMode,
    OrderApprovalRequestedEvent,
    OrderApprovedEvent,
    OrderRejectedEvent,
    RiskRuleTriggeredEvent,
)
from storage.event_store import EventStore
from storage.projections import (
    query_account_positions_snapshot,
    query_approval_queue,
    query_ready_signals_snapshot,
)
from strategy.risk.position_sizing import (
    PositionSizingConfig,
    calculate_position_size,
    rank_risk_factor,
)
from trading_mode import TradingModeSettings

logger = logging.getLogger(__name__)


def _locked(method):
    """Serialize a mutating service method behind self._lock (a re-entrant lock).

    The fast trigger thread (submit_breakout_now) and the main loop (tick,
    approvals) both mutate execution state (_armed, _requested_symbols) and emit
    events; without this they would race on the shared psycopg2 connection and
    the in-memory sets. RLock so a public method may call another (tick ->
    expire_stale_entries) without deadlocking.
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


@dataclass
class ExecutionSettings:
    enabled: bool = True
    auto_approve: bool = False
    max_orders_per_tick: int = 1
    max_concurrent_positions: int = 3
    risk_per_trade_pct: float = 0.01
    default_equity: float = 100_000.0
    # CONCENTRATE-BY-RANK (stop spraying equal size across mediocre gappers — the
    # human's edge is concentration). Only the top-N armed names by rank may enter,
    # and risk is scaled DOWN by rank (rank-1 full, rank-2 half). 0 = off (every
    # armed name sizes equally as before). Plus a hard per-DAY fresh-entry cap.
    concentrate_top_n: int = 0
    max_fresh_entries_per_day: int = 0   # 0 = no daily cap
    # VWAP SELECTION GATE — the one validated entry-quality signal (above-VWAP
    # breakouts reach +1R ~1.5x as often, n=3,110 from the labeler lift report).
    # Every below-VWAP ready signal is shadow-logged (rule_type=vwap_below) for
    # measurement; entries are only SKIPPED when this is True. Fail-open: a signal
    # with above_vwap=None (missing the field) is never blocked.
    require_above_vwap: bool = False
    # QUALITY-GRADE GATE — "trade fewer, higher-quality setups". The per-signal setup
    # grade (A>=0.80, B>=0.65, C>=0.50, else F) is on the ready-signal snapshot; a signal
    # scoring below this is a low-quality/chop setup. Every sub-threshold signal is
    # shadow-logged (rule_type=quality_below) for measurement; entries are only SKIPPED
    # when min_quality_score > 0. Fail-open: a signal with no quality_score is never
    # blocked. NOTE: the grade rewards a clean impulse-pullback structure + RVOL, so it
    # does NOT recognise a vertical catalyst RUNNER (those grade F) — this gate cuts
    # chop, it does not select leading gainers.
    min_quality_score: float = 0.0
    # UNIFIED ENTRY: run the fast path's shared anti-chase gates (over_extended,
    # day-extension, halt) on the LIVE auto path too. These previously ran ONLY on
    # the fast trigger path, leaving live auto entries unprotected (see the
    # consolidation plan). Fail-open on missing data. False = pre-unification.
    unified_entry: bool = False
    # Auto-path FILL MODEL (the last real divergence from the fast path):
    #   "resting"    — rest a buy-limit AT the breakout level; fills only on a
    #                  pullback to it (today's behaviour; misses runners that gap
    #                  up and never return).
    #   "marketable" — limit a hair above the trigger (entry * (1+slippage)); fills
    #                  as price breaks UP through the level (what the fast path
    #                  does), catching runners. The limit caps the fill price, so a
    #                  halt-resume gap-through can't be chased.
    entry_fill_model: str = "resting"
    max_daily_loss_pct: float = 0.03
    # on a daily-loss breach, also flatten open positions + cancel unfilled entries
    flatten_on_breach: bool = True
    # --- entry mechanism (Ross-Cameron-style; all tunable) -----------------
    # reward target as a multiple of risk (entry + reward_multiple * (entry-stop))
    reward_multiple: float = 2.0
    # how the entry order is placed: "limit" rests at the entry trigger so an
    # unfilled order is a real, cancellable state; "market" fills immediately
    entry_order_type: str = "limit"
    # cancel an unfilled entry after this many minutes of resting (the "back
    # out" time box; ~1 bar == 1 minute). 0 disables the timeout. Wall-clock
    # based, so the invalidation guard can run far more often than this.
    entry_timeout_bars: int = 2
    # cancel an unfilled entry if price trades back below the entry trigger by
    # this fraction. NOT 0.0: at zero tolerance any sub-cent wobble below the
    # trigger cancels the entry one tick after it fires, so a breakout that
    # oscillates around the level churns and never holds (observed live). Give
    # it room — the bracket STOP (opening-range low) is the real protection and
    # risk is capped at 1%/trade by sizing. Negative disables price-break cancel.
    entry_invalidate_pct: float = 0.015
    # live-trigger fast path (submit_breakout_now): how far above the trigger to
    # cap the marketable limit so a breakout FILLS on a runner instead of
    # resting forever at the trigger, while still bounding slippage.
    trigger_slippage_pct: float = 0.004
    # don't cancel an entry within this many seconds of arming — a marketable
    # fill needs a moment to confirm at the broker; cancelling first strips its
    # bracket (the naked-stop failure mode).
    entry_grace_seconds: float = 5.0
    # --- account-aware sizing (matters most on a small REAL account) -------
    # one position's dollar value <= this fraction of equity, so a single trade
    # can't exceed buying power (critical at $300: 3 positions must fit, so ~1/3).
    max_position_pct: float = 0.33
    # liquidity cap: shares <= this fraction of the symbol's day volume so at
    # SIZE you don't move the market. 0 = off (irrelevant at small size).
    liquidity_max_volume_pct: float = 0.0
    # HARD dollar-risk ceiling per trade (fixed-fractional with a cap): dollar
    # risk = min(equity*risk_per_trade_pct, max_risk_dollars). Without it the %
    # budget alone let wide-stop names risk ~3x the median (the ~-$1k losers in
    # the loss diagnosis). 0 = off (the % budget governs, e.g. a small account).
    max_risk_dollars: float = 0.0
    # PORTFOLIO gross-notional cap: total $ across ALL open+pending positions must
    # stay under this fraction of equity. Without it, max_concurrent (6) x
    # max_position_pct (0.16) = ~96% gross in correlated small-cap gappers — one
    # regime move hits the whole book together. A later entry is shrunk to fit the
    # remaining budget, or blocked if none remains. 0 = off.
    max_gross_notional_pct: float = 0.60
    # --- backout cooldown (anti-thrash) ------------------------------------
    # after an entry backs out (timeout / price-break) the symbol is benched for
    # this many seconds before it can re-arm. Without it a breakout that won't
    # fill on a marketable limit re-arms every pass and churns (observed live:
    # NEOV armed+backed-out 68x in one session, bloating the order list). 0 = off.
    backout_cooldown_seconds: float = 180.0
    # after this many backouts in a session, bench the symbol for the rest of the
    # day (a setup that has failed to fill repeatedly isn't going to start). 0 = no cap.
    max_backouts_per_symbol: int = 3
    # once a position in a symbol closes at a LOSS, don't re-enter that name this
    # session. Stops the "throw good money after bad" re-entry into a name that
    # already failed (observed live: APWC stopped out -904, was bought back, lost
    # another -1012). Only blocks LOSING exits (see reentry_min_loss_pct) so a
    # quick scratch-then-re-enter winner isn't killed (GRAB +513 in the replay).
    reentry_block_after_exit: bool = True
    # only bench a closed name if it exited DOWN more than this fraction — a real
    # stop-out, not a scratch/win. 0 = bench on any close (the blunt original).
    reentry_min_loss_pct: float = 0.01
    # --- anti-chase / halt protection (the "+100% then 3 halts down" failure) ---
    # Block a live entry whose price has already extended more than this fraction
    # ABOVE the trigger (opening-range high). A clean break crosses the level
    # smoothly (~0% extension and passes); a violent gap-THROUGH — a parabolic
    # spike, or a halt reopening past the level — lands far above it, and chasing
    # that buys the top tick that then reverses/halts down. 0 = off.
    # Default 0.15 calibrated on real fires: historical entries cluster <=9.8%
    # extension, with a clean gap to a disaster tail at 26-64% (NEOV x4 @27%,
    # ICCM @64%/26%, BIRD @30%). 15% sits in that dead zone — it blocks the
    # spike-top chases and passes every normal breakout.
    extension_max_pct: float = 0.15
    # Day-extension ceiling: block when the entry is more than this fraction ABOVE
    # the SESSION OPEN — i.e. the stock is already parabolic on the day. This
    # catches what extension_max_pct can't: a name up huge intraday whose ORB-high
    # break is itself clean (small trigger-extension) but is a chase of a parabolic
    # (e.g. ATPC entered +1.7% above trigger yet +32% above the open, and lost).
    # Default 0.30 calibrated on real fires: normal entries are <=14% above the
    # open, the parabolic tail is 20-83%. 0 = off. Needs day_open from the trigger.
    day_extension_max_pct: float = 0.30
    # Skip entries on a name that looks HALTED: in a trading halt you can't exit
    # and it gaps through your stop on resume. The caller passes a per-symbol
    # halt flag (a bar-gap heuristic over the liquid gappers we trade). This flag
    # only gates whether that signal is honoured.
    halt_guard_enabled: bool = True
    # treat a symbol as halted when, during RTH, its freshest minute bar is older
    # than this many minutes WHILE other names are still printing (LULD halts run
    # >= 5 min, so 3 catches a real halt without tripping on normal IEX lag).
    halt_max_bar_gap_min: float = 3.0
    # --- catalyst dilution veto (Phase 2; ships OFF) -----------------------
    # block an entry when the LLM catalyst advisory says the move is driven by a
    # CONFIRMED dilutive offering (a gap that fades as new shares hit). Requires
    # a catalyst_provider AND a conviction at/above the floor below. Default OFF
    # so the LLM stays advisory-only until its accuracy is trusted.
    catalyst_veto_enabled: bool = False
    catalyst_veto_min_conviction: float = 0.6

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "ExecutionSettings":
        values = dict(os.environ)
        if env is not None:
            values.update(env)

        def flag(key: str, default: str) -> bool:
            return values.get(key, default).strip().lower() in {"1", "true", "yes", "on"}

        def _qfloat(v: str) -> float:
            try:
                return max(0.0, min(1.0, float(v)))
            except (TypeError, ValueError):
                return 0.0

        order_type = values.get("TRADING_ENTRY_ORDER_TYPE", "limit").strip().lower()
        if order_type not in {"limit", "market"}:
            order_type = "limit"

        return cls(
            enabled=flag("TRADING_EXECUTION_ENABLED", "1"),
            auto_approve=flag("TRADING_AUTO_APPROVE", "0"),
            max_orders_per_tick=int(values.get("TRADING_MAX_ORDERS_PER_TICK", "1")),
            max_concurrent_positions=int(
                values.get("TRADING_MAX_CONCURRENT_POSITIONS", "3")
            ),
            risk_per_trade_pct=float(values.get("TRADING_RISK_PER_TRADE_PCT", "0.01")),
            default_equity=float(values.get("TRADING_DEFAULT_EQUITY", "100000")),
            max_daily_loss_pct=float(values.get("TRADING_MAX_DAILY_LOSS_PCT", "0.03")),
            flatten_on_breach=flag("TRADING_FLATTEN_ON_BREACH", "1"),
            reward_multiple=float(values.get("TRADING_REWARD_MULTIPLE", "2.0")),
            entry_order_type=order_type,
            entry_timeout_bars=int(values.get("TRADING_ENTRY_TIMEOUT_BARS", "2")),
            entry_invalidate_pct=float(
                values.get("TRADING_ENTRY_INVALIDATE_PCT", "0.015")
            ),
            trigger_slippage_pct=float(
                values.get("TRADING_TRIGGER_SLIP_PCT", "0.004")
            ),
            entry_grace_seconds=float(values.get("TRADING_ENTRY_GRACE_SECONDS", "5")),
            max_position_pct=float(values.get("TRADING_MAX_POSITION_PCT", "0.33")),
            max_risk_dollars=float(values.get("TRADING_MAX_RISK_DOLLARS", "0.0")),
            concentrate_top_n=int(values.get("TRADING_CONCENTRATE_TOP_N", "0")),
            max_fresh_entries_per_day=int(values.get("TRADING_MAX_FRESH_ENTRIES_PER_DAY", "0")),
            require_above_vwap=flag("TRADING_REQUIRE_ABOVE_VWAP", "1"),
            min_quality_score=_qfloat(values.get("TRADING_MIN_QUALITY_SCORE", "0.0")),
            unified_entry=flag("TRADING_UNIFIED_ENTRY", "1"),
            entry_fill_model=values.get("TRADING_ENTRY_FILL_MODEL", "resting"),
            liquidity_max_volume_pct=float(
                values.get("TRADING_LIQUIDITY_MAX_VOLUME_PCT", "0.0")
            ),
            max_gross_notional_pct=float(
                values.get("TRADING_MAX_GROSS_NOTIONAL_PCT", "0.60")
            ),
            backout_cooldown_seconds=float(
                values.get("TRADING_BACKOUT_COOLDOWN_SECONDS", "180")
            ),
            max_backouts_per_symbol=int(
                values.get("TRADING_MAX_BACKOUTS_PER_SYMBOL", "3")
            ),
            reentry_block_after_exit=flag("TRADING_BLOCK_REENTRY_AFTER_EXIT", "1"),
            reentry_min_loss_pct=float(
                values.get("TRADING_REENTRY_MIN_LOSS_PCT", "0.01")
            ),
            extension_max_pct=float(
                values.get("TRADING_EXTENSION_MAX_PCT", "0.15")
            ),
            day_extension_max_pct=float(
                values.get("TRADING_DAY_EXTENSION_MAX_PCT", "0.30")
            ),
            halt_guard_enabled=flag("TRADING_HALT_GUARD_ENABLED", "1"),
            halt_max_bar_gap_min=float(
                values.get("TRADING_HALT_MAX_BAR_GAP_MIN", "3")
            ),
            catalyst_veto_enabled=flag("NEWS_DILUTION_VETO_ENABLED", "0"),
            catalyst_veto_min_conviction=float(
                values.get("NEWS_DILUTION_VETO_CONVICTION", "0.6")
            ),
        )


class TradingExecutionService:
    """Drives the signal -> approval -> order pipeline each tick."""

    def __init__(
        self,
        store: EventStore,
        executor: AlpacaPaperExecutor | None = None,
        settings: ExecutionSettings | None = None,
        trading_mode: TradingModeSettings | None = None,
        session_id: str | None = None,
        equity: float | None = None,
        price_provider=None,
        now_fn=None,
        catalyst_provider=None,
    ):
        self.store = store
        self.settings = settings or ExecutionSettings.from_env()
        # optional callable: symbol -> catalyst advisory dict (or None), from the
        # LLM enrichment cache. Used ONLY for the (gated) dilution veto. Injected
        # so the execution service never reads the DB itself.
        self.catalyst_provider = catalyst_provider
        self.trading_mode = trading_mode or TradingModeSettings.from_env()
        self.session_id = session_id
        self.equity = equity or self.settings.default_equity
        self.executor = executor or AlpacaPaperExecutor(
            store, session_id=session_id
        )
        self.mode = EventMode.PAPER
        # optional callable: symbol -> latest price, used to invalidate unfilled
        # entries that break back below the entry trigger before filling
        self.price_provider = price_provider
        # injectable clock so the entry timeout is wall-clock based (and so the
        # invalidation check can run far more often than the timeout window
        # without changing the timeout's meaning); tests pass a fake clock
        self._now = now_fn or datetime.now
        # signals we've already requested approval for this session
        self._requested_symbols: set[str] = set()
        # armed (submitted, awaiting fill) entries we may need to cancel.
        # order_id -> {symbol, entry_price, broker_order_id, armed_at, checks}
        self._armed: dict[str, dict] = {}
        # backout cooldown (anti-thrash): symbol -> wall-clock time it may re-arm,
        # and how many times it has backed out this session. A symbol that won't
        # fill is benched instead of re-arming every pass. datetime.max == benched
        # for the rest of the session (hit max_backouts_per_symbol).
        self._cooldown_until: dict[str, datetime] = {}
        self._backout_counts: dict[str, int] = {}
        # re-entry block: symbols whose position has CLOSED this session (detected
        # by a name leaving the broker's held set) — benched from re-entry so we
        # don't buy back a name that just stopped out. _prev_held is the last
        # fresh broker snapshot used to spot departures.
        self._exited_today: set[str] = set()
        self._fresh_entries: dict = {"date": None, "n": 0}  # per-day fresh-entry cap
        self._prev_held: set[str] = set()
        # last-seen unrealized return (fraction) per held symbol — used to decide,
        # when a name leaves the book, whether it exited at a real loss (bench) or
        # a scratch/win (allow re-entry).
        self._held_ret: dict[str, float] = {}
        # daily-loss circuit breaker: once tripped, no new entries this session
        # (a REAL loss is permanent for the session — a bounce can't re-open it)
        self._halted = False
        # transient data-halt: equity unreadable (network/DNS). Blocks new entries
        # WHILE unreadable but RECOVERS on the next good read — a DNS blip must not
        # end the trading day the way a real loss does.
        self._data_halt = False
        # session closed (end-of-day flatten): no new entries for the rest of the day
        self._session_closed = False
        # consecutive equity-read failures; after the limit we pause (recoverable)
        self._equity_read_failures = 0
        self._equity_fail_limit = int(
            os.environ.get("TRADING_EQUITY_FAIL_LIMIT", "5")
        )
        # serializes state mutations across the main loop and the trigger thread
        self._lock = threading.RLock()

    # -- pipeline -----------------------------------------------------------

    def _open_position_count(self) -> int:
        snapshots = query_account_positions_snapshot(
            self.store, broker_name=self.executor.broker_name
        )
        if not snapshots:
            return 0
        return len(snapshots[-1].get("positions") or [])

    def _held_symbols(self) -> set[str]:
        snapshots = query_account_positions_snapshot(
            self.store, broker_name=self.executor.broker_name
        )
        if not snapshots:
            return set()
        return {
            str(p.get("symbol"))
            for p in snapshots[-1].get("positions") or []
            if p.get("symbol")
        }

    def _current_equity(self) -> float:
        """Size off the REAL broker equity, not the $100k default.

        On a $300 live account the default would size $1,000 positions and get
        rejected; this reads actual equity (the get_account read is cached, so
        it's cheap) and caches it on the service for fallback.
        """
        client = getattr(self.executor, "client", None)
        if client is not None and hasattr(client, "get_account"):
            try:
                eq = float((client.get_account() or {}).get("equity") or 0.0)
                if eq > 0:
                    self.equity = eq
                    return eq
            except Exception:  # noqa: BLE001
                pass
        return self.equity

    def _remaining_gross_budget(self, equity: float) -> float:
        """$ of new notional still allowed under the portfolio gross-notional cap
        (cap*equity minus the market value of currently-open positions). Inf when
        the cap is disabled. Caps how big the NEXT entry can be so the whole book
        can't pile into ~100% gross of correlated small-caps."""
        cap = float(self.settings.max_gross_notional_pct or 0.0)
        if cap <= 0:
            return float("inf")
        positions = self._broker_positions() or []
        open_gross = 0.0
        for p in positions:
            try:
                open_gross += abs(float(p.get("market_value") or 0.0))
            except (TypeError, ValueError):
                pass
        return max(0.0, cap * equity - open_gross)

    def _broker_held_symbols(self) -> set[str] | None:
        """Symbols the broker reports as OPEN POSITIONS right now (truth).

        Used to guard entry cancellation: our own order_filled events lag the
        actual fill (they arrive via the 60s account sync), so the entry-timeout
        guard once cancelled an entry it thought was unfilled AFTER it had really
        filled — and cancelling a bracket parent cancels its stop/take-profit
        legs, leaving the position NAKED. This queries the broker directly so a
        filled name is never cancelled. None if positions can't be read (caller
        then falls back to the synced snapshot and stays conservative).
        """
        client = getattr(self.executor, "client", None)
        if client is None or not hasattr(client, "get_positions"):
            return None
        try:
            positions = client.get_positions(fresh=True) or []
            return {str(p.get("symbol")) for p in positions if p.get("symbol")}
        except Exception as exc:  # noqa: BLE001
            logger.warning("broker positions unreadable in entry guard: %s", exc)
            return None

    def _release_and_close(self, client, symbol: str) -> None:
        """Cancel a symbol's resting bracket legs, then market-close it.

        Thin wrapper over the shared helper (runtime/flatten.py) — the held-qty
        403 fix lives in one place, shared with the live exit manager. Fetches
        the symbol's open orders itself (no pre-fetched snapshot at EOD)."""
        cancel_protective_and_close(client, symbol)

    def _flatten_all(self, reason: str) -> dict:
        """Cancel unfilled entries and market-close every open position.

        Used at EOD and when the daily-loss circuit breaker trips: stop the
        bleeding by cancelling resting entries and flattening the book.
        Best-effort — errors are collected, not raised, so one bad symbol can't
        block the rest. Resting bracket legs are cancelled before each close so
        the held quantity is freed (else close_position 403s)."""
        result: dict = {"cancelled_entries": 0, "closed_positions": [], "errors": []}
        # 1) cancel unfilled (armed) entries we are tracking
        for order_id in list(self._armed):
            armed = self._armed.pop(order_id)
            symbol = armed.get("symbol", "")
            try:
                self.executor.cancel_entry(
                    order_id, armed.get("broker_order_id"), symbol, reason
                )
                result["cancelled_entries"] += 1
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"cancel {symbol}: {exc}")
            self._requested_symbols.discard(symbol)
        # 2) market-close open positions
        client = getattr(self.executor, "client", None)
        for symbol in sorted(self._held_symbols()):
            if client is None or not hasattr(client, "close_position"):
                result["errors"].append(f"close {symbol}: no broker client to flatten")
                continue
            try:
                self._release_and_close(client, symbol)
                result["closed_positions"].append(symbol)  # only on a confirmed close
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"close {symbol}: {exc}")
        return result

    @_locked
    def _daily_loss_breach(self) -> bool:
        """True while new entries should be blocked.

        Two distinct halts: a REAL daily-loss breach (``_halted``) is permanent
        for the session so a bounce can't re-open the floodgates; an equity-read
        outage (``_data_halt``) blocks WHILE unreadable but recovers on the next
        good read — a transient DNS/network blip must not end the trading day.
        """
        if self._halted or self._session_closed:
            return True
        max_loss = abs(float(self.settings.max_daily_loss_pct or 0.0))
        if max_loss <= 0:
            return False
        client = getattr(self.executor, "client", None)
        if client is None:
            return False
        try:
            acct = client.get_account()
            equity_raw = acct.get("equity")
            if equity_raw is None:
                raise ValueError("account 'equity' field missing")
            equity = float(equity_raw)
            baseline = float(acct.get("last_equity") or equity)
            # good read -> clear the transient data-halt and resume
            if self._data_halt or self._equity_read_failures:
                if self._data_halt:
                    self.store.emit(
                        RiskRuleTriggeredEvent(
                            timestamp=datetime.now(), mode=self.mode,
                            correlation_id=self.session_id,
                            message="Equity readable again — resuming new entries",
                            rule_type="equity_recovered", rule_value=0.0,
                            current_state={"equity": equity},
                            action_taken="resumed_new_entries",
                        )
                    )
                self._data_halt = False
                self._equity_read_failures = 0
        except Exception as exc:  # noqa: BLE001
            self._equity_read_failures += 1
            logger.warning(
                "equity read failed (%d consecutive): %s",
                self._equity_read_failures, exc,
            )
            # PAUSE (recoverable): block new entries while equity is unreadable,
            # rather than silently disabling the most important safety control —
            # but DO NOT permanently halt the session for a transient outage.
            if self._equity_read_failures >= self._equity_fail_limit and not self._data_halt:
                self._data_halt = True
                self.store.emit(
                    RiskRuleTriggeredEvent(
                        timestamp=datetime.now(),
                        mode=self.mode,
                        correlation_id=self.session_id,
                        message=(
                            f"Equity unreadable {self._equity_read_failures}x — "
                            "pausing new entries until it recovers"
                        ),
                        rule_type="equity_unreadable",
                        rule_value=float(self._equity_read_failures),
                        current_state={"failures": self._equity_read_failures},
                        action_taken="paused_new_entries",
                    )
                )
            return self._data_halt
        if baseline <= 0:
            return False
        pnl_pct = (equity - baseline) / baseline
        if pnl_pct <= -max_loss:
            self._halted = True
            flat = (
                self._flatten_all("daily_loss_circuit_breaker")
                if self.settings.flatten_on_breach
                else {"closed_positions": [], "cancelled_entries": 0}
            )
            extra = ""
            action = "halted_new_entries"
            if self.settings.flatten_on_breach:
                n_closed = len(flat["closed_positions"])
                n_cancel = flat["cancelled_entries"]
                flat_errs = flat.get("errors") or []
                extra = (
                    f" — flattened {n_closed} position(s), "
                    f"cancelled {n_cancel} unfilled entr{'y' if n_cancel == 1 else 'ies'}"
                )
                if flat_errs:
                    extra += f"; FLATTEN INCOMPLETE: {flat_errs}"
                action = "halted_flatten_incomplete" if flat_errs else "halted_and_flattened"
            self.store.emit(
                RiskRuleTriggeredEvent(
                    timestamp=datetime.now(),
                    mode=self.mode,
                    correlation_id=self.session_id,
                    message=(
                        f"Daily-loss circuit breaker tripped: {pnl_pct:+.2%} "
                        f"(limit -{max_loss:.0%}) — halting new entries{extra}"
                    ),
                    rule_type="daily_loss",
                    rule_value=max_loss,
                    current_state={
                        "equity": equity,
                        "baseline_equity": baseline,
                        "pnl_pct": round(pnl_pct, 4),
                        "flatten": flat,
                    },
                    action_taken=action,
                )
            )
        return self._halted

    @_locked
    def _dilution_vetoed(self, symbol: str) -> dict | None:
        """If the gated catalyst veto is on AND the LLM advisory flags a confirmed
        dilutive offering at/above the conviction floor, return the advisory (so
        the caller can emit a risk event + skip). Otherwise None. Never raises —
        a missing/odd advisory simply doesn't veto."""
        if not (self.settings.catalyst_veto_enabled and self.catalyst_provider):
            return None
        try:
            adv = self.catalyst_provider(symbol)
        except Exception:  # noqa: BLE001 — advisory is best-effort, never blocks on its own fault
            return None
        if not adv or not adv.get("is_dilutive"):
            return None
        try:
            conviction = float(adv.get("conviction") or 0.0)
        except (TypeError, ValueError):
            return None
        if conviction >= self.settings.catalyst_veto_min_conviction:
            return adv
        return None

    def _emit_dilution_veto(self, symbol: str, adv: dict) -> None:
        self.store.emit(
            RiskRuleTriggeredEvent(
                timestamp=datetime.now(),
                mode=self.mode,
                correlation_id=self.session_id,
                message=f"{symbol}: entry blocked — confirmed dilutive catalyst",
                rule_type="catalyst_dilution_veto",
                rule_value=float(adv.get("conviction") or 0.0),
                current_state={
                    "symbol": symbol,
                    "catalyst_type": adv.get("catalyst_type"),
                    "sentiment": adv.get("sentiment"),
                    "rationale": adv.get("rationale"),
                },
                action_taken="blocked_dilutive_offering",
            )
        )

    def request_approvals_for_ready_signals(self) -> list[str]:
        """Turn fresh ready signals into approval requests. Returns order ids."""
        if not self.settings.enabled:
            return []

        if self._daily_loss_breach():
            return []

        if self._open_position_count() >= self.settings.max_concurrent_positions:
            self.store.emit(
                RiskRuleTriggeredEvent(
                    timestamp=datetime.now(),
                    mode=self.mode,
                    correlation_id=self.session_id,
                    message="Max concurrent positions reached — no new entries",
                    rule_type="max_concurrent_positions",
                    rule_value=float(self.settings.max_concurrent_positions),
                    current_state={"open_positions": self._open_position_count()},
                    action_taken="skipped_new_entries",
                )
            )
            return []

        held = self._held_symbols()
        pending = {row["symbol"] for row in query_approval_queue(self.store)}
        signals = query_ready_signals_snapshot(self.store, session_id=self.session_id)

        created: list[str] = []
        for signal in signals:
            if len(created) >= self.settings.max_orders_per_tick:
                break
            symbol = signal["symbol"]
            if (symbol in held or symbol in pending
                    or symbol in self._requested_symbols
                    or self._in_cooldown(symbol)        # benched after recent backouts
                    or symbol in self._exited_today):   # already round-tripped today
                continue
            vetoed = self._dilution_vetoed(symbol)
            if vetoed is not None:
                self._emit_dilution_veto(symbol, vetoed)
                self._requested_symbols.add(symbol)  # don't re-evaluate every tick
                continue
            entry = signal.get("entry_price")
            stop = signal.get("stop_loss_price")
            if not entry or not stop or stop >= entry:
                continue

            # VWAP selection gate — the one validated entry-quality signal (above-
            # VWAP breakouts reach +1R ~1.5x as often, n=3,110). Every below-VWAP
            # ready signal is shadow-logged for measurement; the entry is only
            # SKIPPED when require_above_vwap is on. above_vwap is None on signals
            # lacking the field -> fail-open, never block on missing data.
            if signal.get("above_vwap") is False:
                enforced = self.settings.require_above_vwap
                self.store.emit(
                    RiskRuleTriggeredEvent(
                        timestamp=datetime.now(),
                        mode=self.mode,
                        correlation_id=self.session_id,
                        message=(
                            f"{symbol} ready BELOW session VWAP "
                            f"(vwap={signal.get('vwap')}, entry={entry}) — "
                            f"{'skipped' if enforced else 'shadow-logged'}"
                        ),
                        rule_type="vwap_below",
                        rule_value=float(signal.get("vwap") or 0.0),
                        current_state={"symbol": symbol, "entry": float(entry),
                                       "enforced": enforced},
                        action_taken="skipped_entry" if enforced else "shadow_logged",
                    )
                )
                if enforced:
                    self._requested_symbols.add(symbol)  # don't re-evaluate every tick
                    continue

            # QUALITY-GRADE gate — trade fewer, higher-quality setups. Shadow-logged
            # always; entries only SKIPPED when min_quality_score > 0. Fail-open: a
            # signal with no quality_score is never blocked.
            qs = signal.get("quality_score")
            if qs is not None and self.settings.min_quality_score > 0 and qs < self.settings.min_quality_score:
                self.store.emit(
                    RiskRuleTriggeredEvent(
                        timestamp=datetime.now(),
                        mode=self.mode,
                        correlation_id=self.session_id,
                        message=(f"{symbol} ready below quality gate "
                                 f"(score {qs:.2f} < {self.settings.min_quality_score:.2f}) — skipped"),
                        rule_type="quality_below",
                        rule_value=float(qs),
                        current_state={"symbol": symbol, "quality_score": float(qs),
                                       "min": self.settings.min_quality_score},
                        action_taken="skipped_entry",
                    )
                )
                self._requested_symbols.add(symbol)  # don't re-evaluate every tick
                continue

            # Anti-chase / halt gates on the LIVE auto path (the entry-path
            # unification — these ran ONLY on the fast trigger path before). The
            # auto path rests a limit AT the trigger, so it can't chase above it
            # (over_extended is a no-op here); the gate that bites is day-extension:
            # don't enter a name whose breakout level is already parabolic vs the
            # session open. halted=False (a resting limit won't fill mid-halt).
            # Fail-open: a missing day_open skips only the day-extension check.
            if self.settings.unified_entry:
                entry_f0 = float(entry)
                skip = self._anti_chase_skip(entry_f0, entry_f0,
                                             signal.get("day_open"), False)
                if skip is not None:
                    self.store.emit(
                        RiskRuleTriggeredEvent(
                            timestamp=datetime.now(),
                            mode=self.mode,
                            correlation_id=self.session_id,
                            message=(f"{symbol} entry blocked: {skip} "
                                     f"(entry={entry}, day_open={signal.get('day_open')})"),
                            rule_type=skip,
                            rule_value=float(signal.get("day_open") or 0.0),
                            current_state={"symbol": symbol, "entry": entry_f0,
                                           "day_open": signal.get("day_open")},
                            action_taken="skipped_entry",
                        )
                    )
                    self._requested_symbols.add(symbol)  # don't re-evaluate every tick
                    continue

            eq = self._current_equity()
            # cap this entry to BOTH the per-position limit AND the portfolio's
            # remaining gross-notional budget (so the book can't pile into ~100%).
            pos_cap = min(eq * self.settings.max_position_pct,
                          self._remaining_gross_budget(eq))
            # Liquidity cap (unification): on the auto path too, cap shares to a
            # fraction of the name's cumulative volume so a thin cheap name can't be
            # oversized into heavy slippage (the VTAK-style stop that filled far past
            # its level). Fail-open: no cum_volume in the signal -> no cap.
            liq = self.settings.liquidity_max_volume_pct
            cum_vol = signal.get("cum_volume") or 0.0
            max_shares = (int(liq * float(cum_vol))
                          if (self.settings.unified_entry and liq > 0 and cum_vol > 0)
                          else None)
            sizing = calculate_position_size(
                float(entry),
                float(stop),
                equity=eq,
                config=PositionSizingConfig(
                    risk_per_trade_pct=self.settings.risk_per_trade_pct,
                    default_equity=self.settings.default_equity,
                ),
                max_position_value=pos_cap,
                max_shares=max_shares,
                max_risk_dollars=self.settings.max_risk_dollars,
            )
            if sizing.position_size <= 0:
                continue

            entry_f = float(entry)
            stop_f = float(stop)
            risk_per_share = entry_f - stop_f
            # FILL MODEL (unification): rest a limit AT the level (default), or place
            # a marketable limit a hair above the trigger so it fills as price breaks
            # UP through it (the fast path's runner-catching behaviour). The limit
            # caps the fill price either way, so a gap-through can't be chased. R
            # (stop/target) is measured from the level (entry_f), not the limit.
            limit_price = (
                round(entry_f * (1.0 + self.settings.trigger_slippage_pct), 2)
                if self.settings.entry_fill_model == "marketable" else entry_f
            )
            request = ExecutionRequest(
                symbol=symbol,
                side="buy",
                quantity=sizing.position_size,
                entry_price=limit_price,
                stop_loss_price=stop_f,
                take_profit_price=round(
                    entry_f + self.settings.reward_multiple * risk_per_share, 2
                ),
                order_type=self.settings.entry_order_type,
            )
            approval_mode = "auto" if self.settings.auto_approve else "manual"
            self.store.emit(
                OrderApprovalRequestedEvent(
                    timestamp=datetime.now(),
                    mode=self.mode,
                    correlation_id=self.session_id,
                    message=(
                        f"Approval requested ({approval_mode}): buy "
                        f"{request.quantity} {symbol} @ ~{entry} stop {stop}"
                    ),
                    order_id=request.order_id,
                    symbol=symbol,
                    requested_by="trading_execution",
                    approval_mode=approval_mode,
                    execution_mode=self.trading_mode.execution_mode,
                    execution_request=request.to_payload(),
                )
            )
            self._requested_symbols.add(symbol)
            created.append(request.order_id)
            logger.info("approval requested for %s (%s)", symbol, request.order_id)

        return created

    @_locked
    def approve_order(
        self, order_id: str, approved_by: str = "dashboard", notes: str | None = None
    ) -> dict:
        """Approve a pending order and execute it."""
        entry = self._find_pending(order_id)
        if entry is None:
            return {"ok": False, "error": f"order {order_id} not pending"}
        request = ExecutionRequest.from_payload(entry["execution_request"])
        # Per-day fresh-entry cap. Live entries flow through THIS auto-approval
        # path, so the cap must be enforced here (the fast submit_breakout_now
        # path shares the same self._fresh_entries counter). Manual dashboard
        # approvals are a human override and are never capped. Reject (not skip)
        # so a capped order leaves the pending queue instead of re-skipping every
        # tick.
        if approved_by == "auto" and self.settings.max_fresh_entries_per_day > 0:
            today = self._now().date().isoformat()
            if self._fresh_entries.get("date") != today:
                self._fresh_entries = {"date": today, "n": 0}
            if self._fresh_entries["n"] >= self.settings.max_fresh_entries_per_day:
                return self.reject_order(
                    order_id, rejected_by="auto", reason="daily_entry_cap"
                )
        self.store.emit(
            OrderApprovedEvent(
                timestamp=datetime.now(),
                mode=self.mode,
                correlation_id=self.session_id,
                message=f"Order {order_id} approved by {approved_by}",
                order_id=order_id,
                symbol=request.symbol,
                approved_by=approved_by,
                approval_notes=notes,
            )
        )
        result = self.executor.execute(request)
        # If the entry didn't fill immediately (a resting limit at the trigger),
        # arm it so expire_stale_entries() can back out on timeout or a break
        # below the entry. Market orders that fill on submit are never armed.
        # Only arm genuinely OPEN resting entries — never a filled, partial, or
        # rejected order (a rejected entry would otherwise occupy a position slot
        # forever and a partial would be backed out while real shares are held).
        if (
            result.ok
            and result.status in {"new", "accepted", "pending_new", "held", "accepted_for_bidding"}
            and request.entry_price
        ):
            self._armed[order_id] = {
                "symbol": request.symbol,
                "entry_price": float(request.entry_price),
                "broker_order_id": result.broker_order_id,
                "armed_at": self._now(),
                "checks": 0,
            }
        if (
            approved_by == "auto"
            and result.ok
            and self.settings.max_fresh_entries_per_day > 0
            and isinstance(self._fresh_entries.get("n"), int)
        ):
            self._fresh_entries["n"] += 1   # count toward the per-day cap
        return {
            "ok": result.ok,
            "order_id": order_id,
            "broker_order_id": result.broker_order_id,
            "status": result.status,
            "error": result.error,
        }

    def _anti_chase_skip(self, entry_ref: float, trigger: float,
                         day_open: float | None, halted: bool) -> str | None:
        """Shared anti-chase / halt entry gates, used by BOTH the fast trigger path
        and (behind TRADING_UNIFIED_ENTRY) the slow auto path. Returns a skip reason
        or None. Fail-open: a missing/zero day_open skips only the day-extension
        check. For a resting-limit fill (slow path) entry_ref==trigger so
        over_extended is a no-op — day_extension + halt are the gates that bite."""
        ext_max = self.settings.extension_max_pct
        if (ext_max > 0 and trigger and float(trigger) > 0
                and entry_ref > float(trigger) * (1.0 + ext_max)):
            return "over_extended"
        # Day-extension ceiling: the stock is already parabolic on the DAY (far
        # above the session open) — chasing that is the "+100% then halts down"
        # trap even when the trigger break itself looks clean.
        day_max = self.settings.day_extension_max_pct
        if (day_max > 0 and day_open is not None and float(day_open) > 0
                and entry_ref > float(day_open) * (1.0 + day_max)):
            return "over_extended_day"
        # Halt guard: you can't exit during a trading halt and it gaps through the
        # stop on resume — don't enter a name flagged as halted.
        if halted and self.settings.halt_guard_enabled:
            return "halted_symbol"
        return None

    @_locked
    def submit_breakout_now(
        self,
        symbol: str,
        trigger: float,
        stop: float,
        last_price: float | None = None,
        reason: str = "orb_live_break",
        cum_volume: float = 0.0,
        halted: bool = False,
        day_open: float | None = None,
        rank: int = 0,
    ) -> dict:
        """Fire a breakout entry immediately on a LIVE price cross.

        The disciplined-but-fast counterpart to the watcher->approval cadence:
        used by the armed-trigger loop when price crosses a pre-computed
        opening-range high. Runs the SAME risk gates as the slow path (daily-loss
        breaker, max-concurrent, per-symbol dedup, 1%-risk sizing), but submits a
        *marketable* limit (capped a hair above the trigger) so it fills on a
        runner instead of resting at the level. Returns a result dict; ``skipped``
        explains a no-op (the symbol can simply try again next tick).
        """
        if not self.settings.enabled:
            return {"ok": False, "skipped": "disabled"}
        if self._daily_loss_breach():
            return {"ok": False, "skipped": "halted"}
        if self._open_position_count() >= self.settings.max_concurrent_positions:
            return {"ok": False, "skipped": "max_positions"}

        # CONCENTRATE-BY-RANK: only the top-N armed names may enter, and risk is
        # scaled down by rank. Stops the equal-size spray across mediocre gappers
        # (the human's edge is concentration). rank_factor 0 => outside the top-N.
        rank_factor = rank_risk_factor(rank, self.settings.concentrate_top_n)
        if rank_factor <= 0.0:
            return {"ok": False, "skipped": "rank_concentration"}
        # hard per-DAY fresh-entry cap (a day-trader takes a few good shots, not 12)
        cap = self.settings.max_fresh_entries_per_day
        if cap > 0:
            today = self._now().date().isoformat() if hasattr(self._now(), "date") else None
            if today is not None:
                if self._fresh_entries.get("date") != today:
                    self._fresh_entries = {"date": today, "n": 0}
                if self._fresh_entries["n"] >= cap:
                    return {"ok": False, "skipped": "daily_entry_cap"}

        # per-symbol dedup shared with the slow path: never double-enter a name
        # that's already held, pending approval, or requested this session.
        held = self._held_symbols()
        pending = {row["symbol"] for row in query_approval_queue(self.store)}
        if symbol in held or symbol in pending or symbol in self._requested_symbols:
            return {"ok": False, "skipped": "already_active"}
        if self._in_cooldown(symbol):  # benched after recent backouts (anti-thrash)
            return {"ok": False, "skipped": "cooldown"}
        if symbol in self._exited_today:  # already round-tripped today — no re-entry
            return {"ok": False, "skipped": "reentry_blocked"}
        vetoed = self._dilution_vetoed(symbol)  # gated catalyst dilution veto
        if vetoed is not None:
            self._emit_dilution_veto(symbol, vetoed)
            return {"ok": False, "skipped": "dilution_veto"}

        # Use last_price only when it's a real positive tick. `last_price or
        # trigger` would coerce a 0.0/garbage price to the trigger — silently
        # skipping the extension guard AND sizing on a phantom price.
        if last_price is not None and float(last_price) <= 0:
            return {"ok": False, "skipped": "bad_price"}
        entry_ref = float(last_price) if last_price is not None else float(trigger)
        stop_f = float(stop)
        trigger_f = float(trigger)
        if stop_f >= entry_ref or trigger_f <= 0:
            return {"ok": False, "skipped": "bad_geometry"}

        # Anti-chase ceiling: distance of the entry price ABOVE the breakout
        # trigger (ORB high). A clean break crosses the level smoothly (~0%) and
        # passes; a violent gap-THROUGH — an intraday parabolic, or a halt
        # reopening past the level — lands far above it, and chasing that buys
        # the top tick that reverses/halts down. (Overnight +100% gappers are
        # already excluded upstream by the trigger book's gap_max; this catches
        # the intraday chase / gap-through that gap_max can't see.)
        skip = self._anti_chase_skip(entry_ref, trigger_f, day_open, halted)
        if skip is not None:
            return {"ok": False, "skipped": skip}

        eq = self._current_equity()
        liq = self.settings.liquidity_max_volume_pct
        max_shares = int(liq * cum_volume) if (liq > 0 and cum_volume > 0) else None
        # per-position cap AND remaining portfolio gross-notional budget
        pos_cap = min(eq * self.settings.max_position_pct,
                      self._remaining_gross_budget(eq))
        if pos_cap <= 0:
            return {"ok": False, "skipped": "gross_notional_cap"}
        # scale the dollar risk by rank (rank-1 full, rank-2+ half) — both the %
        # budget and the hard $ cap scale together so the result scales by the factor
        sizing = calculate_position_size(
            entry_ref,
            stop_f,
            equity=eq,
            config=PositionSizingConfig(
                risk_per_trade_pct=self.settings.risk_per_trade_pct * rank_factor,
                default_equity=self.settings.default_equity,
            ),
            max_position_value=pos_cap,
            max_shares=max_shares,
            max_risk_dollars=self.settings.max_risk_dollars * rank_factor,
        )
        if sizing.position_size <= 0:
            return {"ok": False, "skipped": "zero_size"}

        # marketable limit: fill now (price is already at/above the trigger), but
        # cap how far above we'll chase so a gap-through doesn't pay any price.
        limit_price = round(
            max(entry_ref, trigger_f) * (1.0 + self.settings.trigger_slippage_pct), 2
        )
        risk_per_share = entry_ref - stop_f
        request = ExecutionRequest(
            symbol=symbol,
            side="buy",
            quantity=sizing.position_size,
            entry_price=limit_price,
            stop_loss_price=stop_f,
            take_profit_price=round(
                entry_ref + self.settings.reward_multiple * risk_per_share, 2
            ),
            order_type="limit",
        )
        # audit trail mirrors the slow path: requested(auto) -> approved -> submit
        self.store.emit(
            OrderApprovalRequestedEvent(
                timestamp=datetime.now(),
                mode=self.mode,
                correlation_id=self.session_id,
                message=(
                    f"Live ORB break ({reason}): buy {request.quantity} {symbol} "
                    f"@ ~{entry_ref:.2f} (trigger {trigger_f:.2f}, stop {stop_f:.2f})"
                ),
                order_id=request.order_id,
                symbol=symbol,
                requested_by="orb_trigger",
                approval_mode="auto",
                execution_mode=self.trading_mode.execution_mode,
                execution_request=request.to_payload(),
            )
        )
        self.store.emit(
            OrderApprovedEvent(
                timestamp=datetime.now(),
                mode=self.mode,
                correlation_id=self.session_id,
                message=f"Order {request.order_id} auto-approved (live ORB break)",
                order_id=request.order_id,
                symbol=symbol,
                approved_by="orb_trigger",
                approval_notes=reason,
            )
        )
        self._requested_symbols.add(symbol)
        result = self.executor.execute(request)
        if (
            result.ok
            and result.status
            in {"new", "accepted", "pending_new", "held", "accepted_for_bidding"}
            and request.entry_price
        ):
            self._armed[request.order_id] = {
                "symbol": symbol,
                "entry_price": float(request.entry_price),
                "broker_order_id": result.broker_order_id,
                "armed_at": self._now(),
                "checks": 0,
            }
        if result.ok and self.settings.max_fresh_entries_per_day > 0:
            if isinstance(self._fresh_entries.get("n"), int):
                self._fresh_entries["n"] += 1   # count toward the per-day cap
        logger.info(
            "live ORB break %s qty=%s -> ok=%s status=%s rank=%s",
            symbol, request.quantity, result.ok, result.status, rank,
        )
        return {
            "ok": result.ok,
            "order_id": request.order_id,
            "broker_order_id": result.broker_order_id,
            "status": result.status,
            "error": result.error,
            "quantity": sizing.position_size,
            "entry": limit_price,
            "stop": stop_f,
        }

    @_locked
    def reject_order(
        self, order_id: str, rejected_by: str = "dashboard", reason: str = "manual"
    ) -> dict:
        entry = self._find_pending(order_id)
        if entry is None:
            return {"ok": False, "error": f"order {order_id} not pending"}
        self.store.emit(
            OrderRejectedEvent(
                timestamp=datetime.now(),
                mode=self.mode,
                correlation_id=self.session_id,
                message=f"Order {order_id} rejected by {rejected_by}: {reason}",
                order_id=order_id,
                symbol=entry.get("symbol") or "",
                rejected_by=rejected_by,
                rejection_reason=reason,
            )
        )
        # allow the symbol to re-signal later
        self._requested_symbols.discard(entry.get("symbol") or "")
        # Tag the result so callers can tell a rejection apart from a real execution
        # (both carry ok=True). Without this, process_auto_approvals()'s list counted
        # cap-rejections as executions, so a tripped daily-entry cap masqueraded as
        # `auto_executed=1` in the loop log with no matching broker order.
        return {"ok": True, "order_id": order_id, "rejected": True, "reason": reason}

    @_locked
    def process_auto_approvals(self) -> list[dict]:
        """Auto-approve any pending requests marked approval_mode=auto."""
        results = []
        for entry in query_approval_queue(self.store):
            if entry.get("approval_mode") == "auto":
                results.append(
                    self.approve_order(entry["order_id"], approved_by="auto")
                )
        return results

    @_locked
    def submit_exit_order(self, symbol: str, reason: str = "manual_exit") -> dict:
        """Close an open broker position for symbol with a market sell."""
        held = self._held_symbols()
        quantity = None
        snapshots = query_account_positions_snapshot(
            self.store, broker_name=self.executor.broker_name
        )
        if snapshots:
            for p in snapshots[-1].get("positions") or []:
                if str(p.get("symbol")) == symbol:
                    quantity = int(abs(float(p.get("quantity") or 0)))
        if symbol not in held or not quantity:
            return {"ok": False, "error": f"no open position for {symbol}"}
        # Prefer close_position: closes the FULL position regardless of long/short
        # sign and fractional qty. A plain 'sell' of abs(qty) would DOUBLE a short
        # position, and int-truncation would leave residual shares un-flat.
        client = getattr(self.executor, "client", None)
        if client is not None and hasattr(client, "close_position"):
            try:
                raw = client.close_position(symbol) or {}
                return {
                    "ok": True,
                    "order_id": str(raw.get("id") or f"close-{symbol}"),
                    "broker_order_id": raw.get("id"),
                    "status": raw.get("status") or "closing",
                    "error": None,
                    "reason": reason,
                }
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": str(exc), "reason": reason}
        request = ExecutionRequest(symbol=symbol, side="sell", quantity=quantity)
        result = self.executor.execute(request)
        return {
            "ok": result.ok,
            "order_id": request.order_id,
            "broker_order_id": result.broker_order_id,
            "status": result.status,
            "error": result.error,
            "reason": reason,
        }

    @_locked
    def close_session(self, reason: str = "eod_flatten") -> dict:
        """End-of-day: block new entries and flatten the book.

        A day-trading strategy shouldn't carry overnight gap risk, so at the
        configured time the loop calls this to cancel resting entries, market-
        close every open position (which also cancels its bracket legs), and
        halt new entries for the rest of the session.
        """
        self._session_closed = True
        result = self._flatten_all(reason)
        # belt-and-suspenders: close any broker position the store snapshot missed
        client = getattr(self.executor, "client", None)
        if client is not None and hasattr(client, "get_positions"):
            try:
                for p in client.get_positions():
                    sym = p.get("symbol")
                    if sym and sym not in result["closed_positions"]:
                        try:
                            self._release_and_close(client, sym)
                            result["closed_positions"].append(sym)
                        except Exception as exc:  # noqa: BLE001
                            result["errors"].append(f"close {sym}: {exc}")
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"positions: {exc}")
        self.store.emit(
            RiskRuleTriggeredEvent(
                timestamp=datetime.now(), mode=self.mode, correlation_id=self.session_id,
                message=(f"End-of-day flatten ({reason}): closed "
                         f"{len(result['closed_positions'])} position(s), "
                         f"cancelled {result['cancelled_entries']} entr"
                         f"{'y' if result['cancelled_entries'] == 1 else 'ies'}"
                         + (f"; ERRORS {result['errors']}" if result['errors'] else "")),
                rule_type="eod_flatten", rule_value=0.0,
                current_state=result, action_taken="closed_session",
            )
        )
        return result

    def flatten_overnight_carries(self, today_et, reason: str = "open_catchup") -> dict:
        """Catch-up flatten: close positions CARRIED from a prior session.

        A day-trading book should never hold overnight, but a missed/failed EOD
        flatten (loop down, or the held-qty 403 before that was fixed) can leave a
        stale carry bleeding for days (ATPC over the 4-day Juneteenth gap). On
        each session this closes any position not entered today — WITHOUT halting
        the session (unlike ``close_session``); today's fresh names are untouched.
        Best-effort: errors are collected, not raised. Returns
        {carries, closed, errors}.
        """
        result: dict = {"carries": [], "closed": [], "errors": []}
        client = getattr(self.executor, "client", None)
        if client is None or not hasattr(client, "close_position"):
            return result
        try:
            positions = client.get_positions()
            open_syms = {p.get("symbol") for p in positions
                         if abs(float(p.get("qty") or 0)) > 0}
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"positions: {exc}")
            return result
        if not open_syms:
            return result
        try:
            orders = client.get_orders(status="all", limit=500, nested=True,
                                       symbols=sorted(open_syms))
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"orders: {exc}")
            return result
        buy_fills = buy_fills_from_orders(orders)
        carries = find_overnight_carries(open_syms, buy_fills, today_et)
        result["carries"] = carries
        for symbol in carries:
            try:
                self._release_and_close(client, symbol)
                result["closed"].append(symbol)
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"close {symbol}: {exc}")
        if carries:
            self.store.emit(
                RiskRuleTriggeredEvent(
                    timestamp=datetime.now(), mode=self.mode, correlation_id=self.session_id,
                    message=(f"Open catch-up flatten ({reason}): carried {carries}; "
                             f"closed {result['closed']}"
                             + (f"; ERRORS {result['errors']}" if result['errors'] else "")),
                    rule_type="open_catchup_flatten", rule_value=0.0,
                    current_state=result, action_taken="flatten_carries",
                )
            )
        return result

    def _filled_order_ids(self) -> set[str]:
        ids: set[str] = set()
        for e in self.store.query_events(event_type="order_filled", limit=None):
            payload = json.loads(e.get("payload_json", "{}"))
            if payload.get("order_id"):
                ids.add(str(payload["order_id"]))
        return ids

    def _cancelled_order_ids(self) -> set[str]:
        ids: set[str] = set()
        for e in self.store.query_events(event_type="order_cancelled", limit=None):
            payload = json.loads(e.get("payload_json", "{}"))
            if payload.get("order_id"):
                ids.add(str(payload["order_id"]))
        return ids

    @_locked
    def _in_cooldown(self, symbol: str) -> bool:
        """True if symbol is benched after recent backouts (anti-thrash)."""
        until = self._cooldown_until.get(symbol)
        if until is None:
            return False
        if self._now() >= until:
            del self._cooldown_until[symbol]  # cooldown elapsed
            return False
        return True

    def _register_backout(self, symbol: str) -> str:
        """Record a backout and bench the symbol; returns a short reason suffix."""
        n = self._backout_counts.get(symbol, 0) + 1
        self._backout_counts[symbol] = n
        cap = self.settings.max_backouts_per_symbol
        if cap and n >= cap:
            self._cooldown_until[symbol] = datetime.max  # benched for the session
            return f"benched for session ({n} backouts)"
        secs = self.settings.backout_cooldown_seconds
        if secs > 0:
            self._cooldown_until[symbol] = self._now() + timedelta(seconds=secs)
            return f"cooldown {secs:.0f}s (backout {n})"
        return f"backout {n}"

    def _broker_positions(self) -> list[dict] | None:
        """Fresh broker positions (full dicts), or None if unreadable."""
        client = getattr(self.executor, "client", None)
        if client is None or not hasattr(client, "get_positions"):
            return None
        try:
            return client.get_positions(fresh=True) or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("broker positions unreadable in re-entry guard: %s", exc)
            return None

    def _note_position_exits(self) -> None:
        """Detect names whose position closed since last pass and bench the ones
        that exited at a real LOSS from re-entry this session — re-buying a name
        that just stopped out is throwing good money after bad. A name that
        scratched or won is left alone (a quick re-entry can be the right call)."""
        if not self.settings.reentry_block_after_exit:
            return
        positions = self._broker_positions()
        if positions is None:   # broker unreadable — don't trust a stale snapshot
            return
        held: set[str] = set()
        for p in positions:
            sym = p.get("symbol")
            if not sym:
                continue
            held.add(sym)
            try:
                self._held_ret[sym] = float(p.get("unrealized_plpc") or 0.0)
            except (TypeError, ValueError):
                pass
        thr = abs(float(self.settings.reentry_min_loss_pct or 0.0))
        for sym in (self._prev_held - held):
            last_ret = self._held_ret.pop(sym, 0.0)
            if thr > 0 and last_ret >= -thr:
                continue  # scratched or won — allow re-entry
            self._exited_today.add(sym)
            self.store.emit(RiskRuleTriggeredEvent(
                timestamp=datetime.now(), mode=self.mode,
                correlation_id=self.session_id,
                message=(f"{sym} closed at {last_ret*100:+.1f}% — benched from "
                         f"re-entry today"),
                rule_type="reentry_block", rule_value=last_ret,
                current_state={"symbol": sym, "exit_return": last_ret},
                action_taken="reentry_blocked",
            ))
        self._prev_held = held

    def expire_stale_entries(self) -> list[dict]:
        """Back out of unfilled entries that timed out or broke their trigger.

        This is the disciplined other half of auto-arming: once a setup is
        recognized we enter with conviction, but if the fill never comes (the
        move didn't follow through) we don't sit there forever — we cancel on a
        time box or when price falls back through the entry, freeing risk
        budget and the concurrent-position slot for the next setup.

        Safe to call as often as you like: the price-break check uses the live
        ``price_provider`` each call, while the timeout is wall-clock based, so
        running this every few seconds reacts fast without shortening the
        timeout window.
        """
        # track positions that have CLOSED since last pass (so a stopped-out name
        # is benched from re-entry). Done BEFORE the early-return so it runs every
        # pass, not only when there are armed entries to expire.
        self._note_position_exits()
        if not self._armed:
            return []

        filled = self._filled_order_ids()
        cancelled = self._cancelled_order_ids()
        # broker truth: never cancel an entry whose position is actually open
        # (cancelling its bracket parent would strip the protective stop/TP).
        # Fall back to the synced snapshot if the broker can't be read.
        broker_held = self._broker_held_symbols()
        if broker_held is None:
            broker_held = self._held_symbols()
        invalidate_pct = self.settings.entry_invalidate_pct
        timeout_bars = self.settings.entry_timeout_bars
        now = self._now()
        actions: list[dict] = []

        for order_id in list(self._armed):
            armed = self._armed[order_id]
            symbol = armed["symbol"]

            # already resolved at the broker -> stop tracking
            if order_id in filled:
                self._armed.pop(order_id, None)
                continue
            if order_id in cancelled:
                self._armed.pop(order_id, None)
                self._requested_symbols.discard(symbol)
                continue
            # FILLED at the broker (position open) -> stop tracking, NEVER cancel
            # (this is the naked-stop fix: cancelling here killed the stop leg).
            if symbol in broker_held:
                self._armed.pop(order_id, None)
                continue
            # grace window: a just-armed entry may still be filling — don't strip
            # its bracket before the fill can confirm (belt to the fresh-positions
            # check above; together they close the naked-stop race)
            if (now - armed["armed_at"]).total_seconds() < self.settings.entry_grace_seconds:
                continue

            armed["checks"] += 1
            reason = None

            # (a) price-break invalidation: traded back below the entry trigger.
            # Uses the live last-trade price, so this reacts tick-by-tick rather
            # than waiting for a bar to close.
            if invalidate_pct >= 0 and self.price_provider is not None:
                try:
                    last = self.price_provider(symbol)
                except Exception:  # noqa: BLE001
                    last = None
                if last is not None:
                    threshold = armed["entry_price"] * (1.0 - invalidate_pct)
                    if float(last) < threshold:
                        reason = (
                            f"entry invalidated: {float(last):.4f} < trigger "
                            f"{threshold:.4f}"
                        )

            # (b) time box: unfilled for too long (wall-clock minutes ~ bars)
            if reason is None and timeout_bars > 0:
                elapsed_min = (now - armed["armed_at"]).total_seconds() / 60.0
                if elapsed_min >= timeout_bars:
                    reason = f"entry timed out: unfilled {elapsed_min:.1f} min"

            if reason is not None:
                self.executor.cancel_entry(
                    order_id, armed.get("broker_order_id"), symbol, reason
                )
                bench = self._register_backout(symbol)  # anti-thrash: cool the name
                self.store.emit(
                    RiskRuleTriggeredEvent(
                        timestamp=datetime.now(),
                        mode=self.mode,
                        correlation_id=self.session_id,
                        message=f"Backed out of {symbol} entry — {reason} [{bench}]",
                        rule_type="entry_backout",
                        rule_value=float(timeout_bars),
                        current_state={
                            "symbol": symbol,
                            "order_id": order_id,
                            "checks": armed["checks"],
                            "backouts": self._backout_counts.get(symbol, 0),
                        },
                        action_taken="cancelled_unfilled_entry",
                    )
                )
                self._armed.pop(order_id, None)
                self._requested_symbols.discard(symbol)
                actions.append({"order_id": order_id, "symbol": symbol,
                                "reason": reason, "cooldown": bench})

        return actions

    @_locked
    def tick(self) -> dict:
        """One execution pass: expire stale entries, request approvals, auto-execute."""
        backed_out = self.expire_stale_entries()
        requested = self.request_approvals_for_ready_signals()
        auto = self.process_auto_approvals() if self.settings.auto_approve else []
        return {
            "approvals_requested": requested,
            "auto_executed": auto,
            "backed_out": backed_out,
        }

    # -- helpers --------------------------------------------------------------

    def _find_pending(self, order_id: str) -> dict | None:
        for entry in query_approval_queue(self.store):
            if str(entry.get("order_id")) == str(order_id):
                return entry
        return None
