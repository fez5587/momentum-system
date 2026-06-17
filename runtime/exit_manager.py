"""Live exit manager — actively manages OPEN positions per strategy/exits.

The static bracket (fixed stop + fixed target) is replaced/augmented here with
the SAME managed-exit rules the backtest swept (strategy/exits.manage_live):
move the stop to breakeven, trail it, scale out, or exit on a first red candle.

Critically, the stop is RATCHETED via a broker order REPLACE (PATCH), never a
cancel — cancelling a bracket leg strips the protection (the naked-stop bug).
Runs on the main loop (not the fast trigger thread): stops update off closed
bars, so 10-15s cadence is plenty, and it keeps slow broker calls off the hot
path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from storage.event_schema import EventMode, RiskRuleTriggeredEvent
from strategy.exits import ExitConfig, TRAIL_NONE, manage_live

logger = logging.getLogger(__name__)


@dataclass
class _Tracked:
    entry_price: float
    init_stop: float      # original opening-range-low stop (for the R math)
    entry_ts: object      # timestamp we first saw the position (~entry)
    last_stop: float      # last stop we set at the broker
    scaled: bool = False


def _is_active(cfg: ExitConfig) -> bool:
    return bool(cfg and (cfg.breakeven_at_r or cfg.trail_mode != TRAIL_NONE
                         or cfg.scale_out_r or cfg.first_red_exit))


class LiveExitManager:
    def __init__(self, client, store, bars_fn, cfg: ExitConfig | None = None,
                 session_id=None, mode: EventMode = EventMode.PAPER):
        self.client = client
        self.store = store
        self.bars_fn = bars_fn   # (symbol) -> today's RTH minute bars DataFrame
        self.cfg = cfg or ExitConfig.from_env()
        self.session_id = session_id
        self.mode = mode
        self._tracked: dict[str, _Tracked] = {}

    # -- broker helpers ----------------------------------------------------

    def _stop_leg(self, symbol: str):
        """(order_id, stop_price) of the live sell-stop protecting symbol."""
        try:
            orders = self.client.get_orders(status="open", limit=200, nested=True)
        except Exception:  # noqa: BLE001
            return (None, None)
        for o in orders:
            for c in [o, *(o.get("legs") or [])]:
                sym = c.get("symbol") or o.get("symbol")
                if (sym == symbol and c.get("type") in ("stop", "stop_limit")
                        and c.get("side") == "sell"):
                    sp = c.get("stop_price")
                    return (c.get("id"), float(sp) if sp else None)
        return (None, None)

    def _emit(self, symbol: str, message: str, rule: str, state: dict) -> None:
        from datetime import datetime
        self.store.emit(RiskRuleTriggeredEvent(
            timestamp=datetime.now(), mode=self.mode, correlation_id=self.session_id,
            message=message, rule_type=rule, rule_value=0.0,
            current_state={"symbol": symbol, **state}, action_taken=rule,
        ))

    # -- main pass ---------------------------------------------------------

    def manage(self) -> list[str]:
        if not _is_active(self.cfg):
            return []  # static bracket — broker OCO handles it, nothing to do
        try:
            positions = self.client.get_positions()
        except Exception:  # noqa: BLE001
            return []
        held = {p.get("symbol"): p for p in positions if p.get("symbol")}
        for sym in list(self._tracked):  # stop tracking closed names
            if sym not in held:
                self._tracked.pop(sym, None)

        actions: list[str] = []
        for sym, p in held.items():
            try:
                bars = self.bars_fn(sym)
            except Exception:  # noqa: BLE001
                continue
            if bars is None or len(bars) == 0:
                continue
            entry = float(p.get("avg_entry_price") or 0.0)
            if entry <= 0:
                continue
            if sym not in self._tracked:
                _leg, cur_stop = self._stop_leg(sym)
                if cur_stop is None or cur_stop >= entry:
                    continue  # no usable protective stop yet (or above entry)
                self._tracked[sym] = _Tracked(
                    entry, cur_stop, _last_ts(bars), cur_stop)
            tr = self._tracked[sym]

            since = _bars_since(bars, tr.entry_ts)
            if len(since) == 0:
                since = bars
            d = manage_live(tr.entry_price, tr.init_stop, since, self.cfg, tr.scaled)

            # (1) first-red: exit the whole position now
            if d.exit_now:
                try:
                    self.client.close_position(sym)
                    self._emit(sym, f"first-red exit {sym} @~mkt", "exit_first_red",
                               {"reason": d.reason})
                    actions.append(f"{sym} first-red exit")
                    self._tracked.pop(sym, None)
                    continue
                except Exception as exc:  # noqa: BLE001
                    logger.warning("first-red close failed %s: %s", sym, exc)

            # (2) scale-out a fraction into strength
            if d.scale_out_frac > 0 and not tr.scaled:
                qty = int(abs(float(p.get("qty") or 0)))
                sell = int(qty * d.scale_out_frac)
                if sell >= 1:
                    try:
                        self.client.close_position(sym, qty=sell)
                        tr.scaled = True
                        self._emit(sym, f"scaled out {sell} {sym}", "exit_scale_out",
                                   {"qty": sell})
                        actions.append(f"{sym} scaled {sell}")
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("scale-out failed %s: %s", sym, exc)

            # (3) ratchet the protective stop UP (breakeven / trail) via replace
            leg_id, cur_stop = self._stop_leg(sym)
            if leg_id and cur_stop is not None and d.desired_stop > cur_stop + 0.01:
                try:
                    self.client.replace_order(leg_id, stop_price=round(d.desired_stop, 2))
                    tr.last_stop = d.desired_stop
                    self._emit(sym, f"stop {sym} {cur_stop:.2f} -> {d.desired_stop:.2f}",
                               "stop_moved", {"from": cur_stop, "to": d.desired_stop})
                    actions.append(f"{sym} stop->{d.desired_stop:.2f}")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("stop move failed %s: %s", sym, exc)
        return actions


def _last_ts(bars: pd.DataFrame):
    try:
        return pd.Timestamp(bars["timestamp"].iloc[-1])
    except Exception:  # noqa: BLE001
        return None


def _bars_since(bars: pd.DataFrame, ts) -> pd.DataFrame:
    if ts is None or "timestamp" not in bars.columns:
        return bars
    try:
        return bars[pd.to_datetime(bars["timestamp"]) >= pd.Timestamp(ts)].reset_index(drop=True)
    except Exception:  # noqa: BLE001
        return bars
