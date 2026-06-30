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

from runtime.flatten import cancel_protective_and_close
from storage.event_schema import EventMode, RiskRuleTriggeredEvent
from strategy.exits import ExitConfig, TRAIL_NONE, catastrophe_triggered, manage_live

logger = logging.getLogger(__name__)


@dataclass
class _Tracked:
    entry_price: float
    init_stop: float      # original opening-range-low stop (for the R math)
    entry_ts: object      # timestamp we first saw the position (~entry)
    scaled: bool = False


def _is_active(cfg: ExitConfig) -> bool:
    return bool(cfg and (cfg.breakeven_at_r or cfg.breakeven_at_pct
                         or cfg.trail_mode != TRAIL_NONE
                         or cfg.scale_out_r or cfg.first_red_exit
                         or cfg.profit_lock_tiers or cfg.catastrophe_pct
                         or cfg.enforce_stop_grace_passes))


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
        self._naked_passes: dict[str, int] = {}   # consecutive passes a held position had no live stop
        self._open_anchored: set[str] = set()     # breakeven carries re-anchored to today's opening range

    # -- broker helpers ----------------------------------------------------

    # a bracket stop leg sits in these states once the entry has filled; it is a
    # child of the (now FILLED) entry, so a status="open" query misses it.
    _ACTIVE = {"held", "new", "accepted", "pending_new",
               "accepted_for_bidding", "partially_filled"}

    def _all_orders(self, symbols=None):
        # scope to the held symbols — Alpaca's `limit` keeps only the most-recent
        # orders, so on a busy day an unscoped query drops older positions' stop
        # legs and we'd misread a protected position as naked.
        try:
            return self.client.get_orders(status="all", limit=500, nested=True,
                                          symbols=list(symbols) if symbols else None)
        except Exception:  # noqa: BLE001
            return None

    def _stop_leg(self, symbol: str, orders):
        """(order_id, stop_price) of the live sell-stop protecting symbol.

        Uses pre-fetched ``orders`` (status="all" — the protective leg is a
        'held' child of the FILLED entry, which status="open" wouldn't surface).
        """
        if orders is None:
            return (None, None)
        for o in orders:
            for c in [o, *(o.get("legs") or [])]:
                sym = c.get("symbol") or o.get("symbol")
                if (sym == symbol and c.get("type") in ("stop", "stop_limit")
                        and c.get("side") == "sell"
                        and c.get("status") in self._ACTIVE):
                    sp = c.get("stop_price")
                    return (c.get("id"), float(sp) if sp else None)
        return (None, None)

    def _flatten(self, symbol: str, orders) -> None:
        """Market-close a position, cancelling its resting protective legs first.

        Delegates to the shared helper so the held-qty 403 fix lives in exactly
        one place (see runtime/flatten.py). ``orders`` is the pass's pre-fetched
        nested snapshot, reused to avoid a redundant fetch."""
        cancel_protective_and_close(self.client, symbol, orders=orders)

    def _market_open(self) -> bool:
        """Broker clock — is the regular session open? A market FLATTEN can't fill
        when closed (it just churns submit/cancel every pass), so the naked-stop
        enforcement rests a protective stop instead while closed. Falls back to an
        ET wall-clock check if the clock call fails; assumes OPEN if even that fails
        (flatten is the safe intraday default)."""
        try:
            return bool(self.client.get_clock().get("is_open"))
        except Exception:  # noqa: BLE001
            try:
                from datetime import datetime
                from zoneinfo import ZoneInfo
                now = datetime.now(ZoneInfo("America/New_York"))
                return now.weekday() < 5 and (9, 30) <= (now.hour, now.minute) and now.hour < 16
            except Exception:  # noqa: BLE001
                return True

    def _rest_protective_stop(self, symbol: str, p: dict, entry: float, cur: float) -> float:
        """Place a resting sell-stop on a NAKED long so it survives after-hours and
        guards the open — at BREAKEVEN when in profit; a catastrophe-distance stop just
        below market when underwater (a sell-stop can't sit above the last price).
        Returns the stop price placed."""
        qty = int(abs(float(p.get("qty") or 0)))
        if qty < 1:                            # degenerate qty: a 0-qty submit is
            return round(entry, 2)             # rejected and would churn — bail
        # cur falsy (price missing / 0 after-hours) falls to the breakeven arm; a stop
        # at entry is below market when in profit and a safe floor otherwise.
        if cur and cur <= entry:
            pct = self.cfg.catastrophe_pct or 0.10
            stop_px = round(cur * (1 - pct), 2)
        else:
            stop_px = round(entry, 2)          # breakeven
        self.client.submit_order(symbol=symbol, qty=qty, side="sell",
                                 order_type="stop", stop_price=stop_px, time_in_force="gtc")
        return stop_px

    def _entry_time(self, symbol: str, orders):
        """UTC-naive fill time of the most recent FILLED buy entry for symbol."""
        if orders is None:
            return None
        best = None
        for o in orders:
            if (o.get("symbol") == symbol and o.get("side") == "buy"
                    and float(o.get("filled_qty") or 0) > 0):
                ft = o.get("filled_at") or o.get("submitted_at")
                if not ft:
                    continue
                try:
                    ts = pd.Timestamp(ft)
                    if ts.tzinfo is not None:
                        ts = ts.tz_convert("UTC").tz_localize(None)
                except Exception:  # noqa: BLE001
                    continue
                if best is None or ts > best:
                    best = ts
        return best

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
                self._open_anchored.discard(sym)
        if not held:
            return []
        # fetch the orders snapshot ONCE per pass (status="all" is the heavy call;
        # reuse it for every position's stop-leg + entry-time lookups), scoped to
        # the held symbols so a busy day's order volume can't truncate the legs.
        orders = self._all_orders(held.keys())
        mkt_open = self._market_open()   # once per pass — gates flatten-vs-rest + trailing

        actions: list[str] = []
        for sym, p in held.items():
            entry = float(p.get("avg_entry_price") or 0.0)
            cur = float(p.get("current_price") or 0.0)
            is_long = str(p.get("side") or "long").lower() != "short"
            leg_id, cur_stop = self._stop_leg(sym, orders)  # live protective sell-stop

            # CATASTROPHE STOP — the hard backstop, FIRST and unconditional. Runs on
            # EVERY held position, before the bars fetch and before the no-stop skip
            # below — the exact NIVF case (ran to -23% with no live stop = ~the
            # entire net loss). Doesn't depend on bars.
            if (self.cfg.catastrophe_pct and entry > 0 and cur > 0 and is_long
                    and catastrophe_triggered(entry, cur, cur_stop,
                                              self.cfg.catastrophe_pct,
                                              self.cfg.catastrophe_risk_mult)):
                loss_pct = (entry - cur) / entry * 100
                try:
                    if mkt_open:
                        self._flatten(sym, orders)
                        self._emit(sym, f"CATASTROPHE exit {sym} @~{cur:.4f} "
                                   f"({loss_pct:.1f}% below entry)", "exit_catastrophe",
                                   {"price": cur, "entry": entry, "stop": cur_stop})
                        actions.append(f"{sym} CATASTROPHE-exit @{cur:.2f} (-{loss_pct:.0f}%)")
                    elif cur_stop is None:
                        # closed + naked: a market flatten can't fill — rest a protective
                        # stop below market so it fires at the open instead of churning.
                        stop_px = self._rest_protective_stop(sym, p, entry, cur)
                        self._emit(sym, f"CATASTROPHE protect {sym} @{stop_px:.2f} (market "
                                   f"closed — rested a stop, {loss_pct:.1f}% below entry)",
                                   "exit_catastrophe", {"price": cur, "entry": entry, "stop": stop_px})
                        actions.append(f"{sym} CATASTROPHE rest-stop @{stop_px:.2f} (closed)")
                    else:
                        # closed but already has a stop — it fires at the open; don't
                        # market-churn and don't double up the protection.
                        continue
                    self._tracked.pop(sym, None)
                    self._naked_passes.pop(sym, None)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("catastrophe enforcement failed %s: %s", sym, exc)
                continue

            # STOP ENFORCEMENT — a held position MUST have a live protective stop.
            # If the bracket's stop leg never attached or was stripped (NAKED), the
            # old code SILENTLY SKIPPED management and let it run to EOD (NIVF).
            # Flatten after a short grace (consecutive passes) so a just-filled
            # bracket's legs can attach first.
            grace = self.cfg.enforce_stop_grace_passes
            if grace and entry > 0 and leg_id is None:
                n = self._naked_passes.get(sym, 0) + 1
                self._naked_passes[sym] = n
                if n >= grace:
                    try:
                        if mkt_open:
                            # session open: a market flatten fills — the safe exit.
                            self._flatten(sym, orders)
                            self._emit(sym, f"naked-stop enforced exit {sym} "
                                       f"(no live stop, {n} passes)", "exit_naked_stop",
                                       {"passes": n, "price": cur, "entry": entry})
                            actions.append(f"{sym} naked-stop exit (no live stop)")
                        else:
                            # closed: a market sell can't fill (it just churns every
                            # pass) — REST a protective stop so the position survives
                            # to the open protected, and the next pass sees the stop
                            # and stops enforcing.
                            stop_px = self._rest_protective_stop(sym, p, entry, cur)
                            self._emit(sym, f"naked-stop PROTECT {sym} @{stop_px:.2f} "
                                       f"(market closed — rested a stop, {n} passes)",
                                       "exit_naked_stop", {"passes": n, "stop": stop_px,
                                                           "entry": entry})
                            actions.append(f"{sym} rested protective stop @{stop_px:.2f} (closed)")
                        self._tracked.pop(sym, None)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("naked-stop enforcement failed %s: %s", sym, exc)
                    self._naked_passes.pop(sym, None)
                continue  # don't run trail logic on a naked position
            self._naked_passes.pop(sym, None)  # has a live stop

            # TRAILING runs only while the session is OPEN: after-hours there are no new
            # highs to trail under, and a resting GTC stop is often not replaceable yet
            # ("accepted" status -> Alpaca 422 on replace), so attempting it just churns.
            # The stop stays protective; it ratchets up at the open.
            if not mkt_open:
                continue

            try:
                bars = self.bars_fn(sym)
            except Exception:  # noqa: BLE001
                continue
            if bars is None or len(bars) == 0:
                continue
            if sym not in self._tracked:
                if cur_stop is None:
                    continue  # naked — handled by the enforcement above
                # trail from the ACTUAL entry fill time (so high-water/R reflect
                # the move since entry, not since we first noticed the position)
                entry_ts = self._entry_time(sym, orders) or _last_ts(bars)
                if cur_stop >= entry:
                    # stop is at/above entry (breakeven reached, OR the original R was
                    # lost — stripped/invalid), so there's no real risk distance to trail
                    # from. RE-ANCHOR the +0.25R ladder to TODAY's opening range (ref =
                    # the open, R = the OR height) so it tracks today's move, not the stale
                    # overnight entry. Until the OR forms, trail off a synthetic R off
                    # entry, then re-anchor. Only ever ratchets UP. 0 = leave it frozen.
                    if not self.cfg.default_trail_r_pct:
                        continue
                    anchor = _open_range_anchor(bars)
                    if anchor:
                        ref, init, entry_ts = anchor
                        self._open_anchored.add(sym)
                    else:
                        ref, init = entry, entry * (1.0 - self.cfg.default_trail_r_pct)
                    self._tracked[sym] = _Tracked(ref, init, entry_ts)
                else:
                    self._tracked[sym] = _Tracked(entry, cur_stop, entry_ts)
            elif (sym not in self._open_anchored and cur_stop is not None
                  and cur_stop >= entry and self.cfg.default_trail_r_pct):
                # tracked on the synthetic R before the opening range formed — re-anchor
                # to the OR now that it has (one-shot; the stop only ratchets up).
                anchor = _open_range_anchor(bars)
                if anchor:
                    ref, init, ots = anchor
                    self._tracked[sym] = _Tracked(ref, init, ots)
                    self._open_anchored.add(sym)
            tr = self._tracked[sym]

            since = _bars_since(bars, tr.entry_ts)
            if len(since) == 0:
                since = bars
            d = manage_live(tr.entry_price, tr.init_stop, since, self.cfg, tr.scaled)

            # (1) first-red: exit the whole position now
            if d.exit_now:
                try:
                    self._flatten(sym, orders)
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
            leg_id, cur_stop = self._stop_leg(sym, orders)
            if cur_stop is not None and d.desired_stop > cur_stop + 0.01:
                last_px = float(p.get("current_price") or 0.0)
                if last_px > 0 and d.desired_stop >= last_px:
                    # the trail is already BREACHED (price fell back through the
                    # prior swing low) — a sell-stop above market would be
                    # rejected, so exit at market now (this is the trail firing).
                    try:
                        self._flatten(sym, orders)
                        self._emit(sym, f"trail breached, exit {sym} @~{last_px:.2f}",
                                   "exit_trail", {"trail": d.desired_stop, "price": last_px})
                        actions.append(f"{sym} trail-exit @{last_px:.2f}")
                        self._tracked.pop(sym, None)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("trail exit failed %s: %s", sym, exc)
                elif leg_id:
                    try:
                        self.client.replace_order(leg_id, stop_price=round(d.desired_stop, 2))
                        self._emit(sym, f"stop {sym} {cur_stop:.2f} -> {d.desired_stop:.2f}",
                                   "stop_moved", {"from": cur_stop, "to": d.desired_stop})
                        actions.append(f"{sym} stop->{d.desired_stop:.2f}")
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("stop move failed %s: %s", sym, exc)
        return actions


def _open_range_anchor(bars: pd.DataFrame, orb_bars: int = 5):
    """Re-anchor a carried position's +0.25R trail to TODAY's OPENING RANGE: reference =
    the session open, R = the first ``orb_bars`` bars' high-low. So the step-trail ladders
    the stop up FROM THE OPEN (tracking today's move) instead of the stale overnight entry.
    Returns (ref, init_stop, open_ts), or None until the range has formed / if degenerate."""
    try:
        if bars is None or len(bars) < orb_bars:
            return None
        first = bars.iloc[:orb_bars]
        hi = float(first["high"].astype(float).max())
        lo = float(first["low"].astype(float).min())
        r = hi - lo
        if r <= 0:
            return None
        o = bars.iloc[0]
        ref = float(o["open"]) if "open" in bars.columns else float(o["close"])
        return (ref, ref - r, pd.Timestamp(o["timestamp"]))
    except Exception:  # noqa: BLE001
        return None


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
