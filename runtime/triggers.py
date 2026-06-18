"""Armed opening-range-breakout triggers — the fast, top-N watchlist.

The watcher path reacts only after a 1-minute candle *closes* above the
breakout level and then walks the ingest->watch->execute cadence (up to a
couple of minutes), and it rests a limit *at* the trigger that a fast runner
never comes back to fill. Both make us late and make us miss the move.

This book is the fix. It keeps the handful of most-promising gappers "armed":
the trigger (opening-range high) and stop (opening-range low) are pre-computed
the moment the opening range finishes forming, so the live loop can fire the
instant the *live* price crosses the trigger — the systematic version of Ross
Cameron's "enter when the candle is breaking, on faith that the volume is
there." Pure state + decision logic; the loop owns I/O (prices, orders).

State machine per symbol:

    waiting  - opening range not complete yet (before ~09:35 ET)
    armed    - range complete and gap/rvol/range qualify -> may fire on a cross
    weak     - range complete but the setup is too soft to fire (shown, greyed)
    fired    - a breakout order has been submitted
    filled   - the position is open at the broker
"""

from __future__ import annotations

import functools
import threading
import time
from dataclasses import dataclass, field

WAITING = "waiting"
ARMED = "armed"
WEAK = "weak"
FIRED = "fired"
FILLED = "filled"

# states that represent a committed trade — pinned across re-ranking so a name
# that fired doesn't get bumped off the board when the gapper ranking rotates
_COMMITTED = (FIRED, FILLED)


def _synced(method):
    """Serialize a book method behind self._lock so the main loop's arm() and
    the trigger thread's poll/fire never iterate ``triggers`` while it's being
    rebuilt."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


@dataclass
class ArmedTrigger:
    symbol: str
    gap_pct: float = 0.0
    rvol: float = 0.0
    trigger: float | None = None  # opening-range high (entry breakout level)
    stop: float | None = None     # opening-range low, cushioned (protective stop)
    range_pct: float = 0.0        # (high-low)/high — how wide the opening range is
    price: float | None = None    # latest live trade price
    price_ts: float = 0.0         # monotonic time of that price (staleness guard)
    cum_volume: float = 0.0       # session cumulative volume (for liquidity sizing)
    dollar_vol: float = 0.0       # recent $-volume (liquidity gate: skip thin spikes)
    catalyst: str = ""            # fresh news headline (if any) — why it's hot
    state: str = WAITING
    rank: int = 0

    def distance_pct(self) -> float | None:
        """How far live price is from the trigger, as a fraction.

        Negative = still below the trigger (waiting for the break); >= 0 = at or
        through it. None until we have both a price and a trigger.
        """
        if self.price and self.trigger:
            return (self.price - self.trigger) / self.trigger
        return None

    def as_dict(self) -> dict:
        d = self.distance_pct()
        return {
            "symbol": self.symbol,
            "state": self.state,
            "rank": self.rank,
            "gap": round(self.gap_pct, 2),
            "rvol": round(self.rvol, 2),
            "trigger": round(self.trigger, 4) if self.trigger else None,
            "stop": round(self.stop, 4) if self.stop else None,
            "price": round(self.price, 4) if self.price else None,
            "dist": round(d, 5) if d is not None else None,
            "range_pct": round(self.range_pct, 4),
            "catalyst": self.catalyst,
        }


@dataclass
class ArmedTriggerBook:
    """Tracks the top ``max_armed`` gappers and decides when each may fire."""

    max_armed: int = 6
    gap_min: float = 3.0        # min overnight gap % to be eligible to fire
    gap_max: float = 1e9        # max gap % — skip blow-off gappers (they fade)
    rvol_min: float = 2.0       # min relative volume to be eligible to fire
    min_range_pct: float = 0.004  # opening range must be at least this wide
    # min recent $-volume to fire. rvol is RELATIVE, so 2x of near-nothing still
    # qualifies — thin names then fill at the top of a one-bar spike and reverse
    # (observed live: WKSP/LNKS/UWMC entered on 100-400 share bars). 0 = off.
    min_dollar_vol: float = 0.0
    # max age (s) of the last price before a trigger may fire. A stalled feed
    # would otherwise let a name fire on a frozen quote into a reverted market.
    # 0 = off.
    max_price_age_s: float = 0.0
    triggers: dict[str, ArmedTrigger] = field(default_factory=dict)
    _lock: object = field(default_factory=threading.RLock, repr=False, compare=False)

    def _eligible(self, t: ArmedTrigger) -> bool:
        return (
            t.trigger is not None
            and t.stop is not None
            and t.stop < t.trigger
            and self.gap_min <= t.gap_pct <= self.gap_max
            and t.rvol >= self.rvol_min
            and t.range_pct >= self.min_range_pct
            and t.dollar_vol >= self.min_dollar_vol
        )

    @_synced
    def arm(self, candidates: list[dict]) -> None:
        """Refresh the book from a ranked candidate list.

        ``candidates`` are dicts (best first):
            {symbol, gap_pct, rvol, trigger, stop, range_pct, complete}

        Symbols already ``fired``/``filled`` are pinned (kept regardless of the
        new ranking); the remaining ``max_armed`` slots are filled from the top
        of ``candidates`` and (re)classified waiting/armed/weak.
        """
        pinned = {s: t for s, t in self.triggers.items() if t.state in _COMMITTED}
        new: dict[str, ArmedTrigger] = dict(pinned)
        rank = 0
        for c in candidates:
            sym = c["symbol"]
            if sym in new:  # already pinned/committed
                continue
            active = sum(1 for t in new.values() if t.state not in _COMMITTED)
            if active >= self.max_armed:
                break
            rank += 1
            t = self.triggers.get(sym) or ArmedTrigger(symbol=sym)
            t.gap_pct = float(c.get("gap_pct") or 0.0)
            t.rvol = float(c.get("rvol") or 0.0)
            t.trigger = c.get("trigger")
            t.stop = c.get("stop")
            t.range_pct = float(c.get("range_pct") or 0.0)
            t.cum_volume = float(c.get("cum_volume") or 0.0)
            t.dollar_vol = float(c.get("dollar_vol") or 0.0)
            t.catalyst = c.get("catalyst") or ""
            t.rank = rank
            if not c.get("complete") or t.trigger is None:
                t.state = WAITING
            elif self._eligible(t):
                t.state = ARMED
            else:
                t.state = WEAK
            new[sym] = t
        self.triggers = new

    @_synced
    def update_price(self, symbol: str, price: float | None) -> None:
        t = self.triggers.get(symbol)
        if t is None:
            return
        if price is None:
            t.price = None       # failed fetch -> clear, so we can't fire on stale
        else:
            t.price = float(price)
            t.price_ts = time.monotonic()

    @_synced
    def fires(self) -> list[ArmedTrigger]:
        """Armed triggers whose live price has reached or crossed the trigger —
        and whose price is fresh (a stalled feed must not fire on a frozen quote)."""
        now = time.monotonic()
        max_age = self.max_price_age_s
        return [
            t
            for t in self.triggers.values()
            if t.state == ARMED
            and t.price is not None
            and t.trigger is not None
            and t.price >= t.trigger
            and (max_age <= 0 or (now - t.price_ts) <= max_age)
        ]

    @_synced
    def mark_fired(self, symbol: str) -> None:
        t = self.triggers.get(symbol)
        if t is not None:
            t.state = FIRED

    @_synced
    def mark_filled(self, held_symbols) -> None:
        """Promote tracked symbols that are now open broker positions to filled."""
        held = set(held_symbols or ())
        for sym, t in self.triggers.items():
            if sym in held:
                t.state = FILLED

    @_synced
    def snapshot(self) -> list[dict]:
        """Board-ready rows, ordered armed -> fired -> filled -> waiting -> weak."""
        order = {ARMED: 0, FIRED: 1, FILLED: 2, WAITING: 3, WEAK: 4}
        return [
            t.as_dict()
            for t in sorted(
                self.triggers.values(),
                key=lambda t: (order.get(t.state, 9), t.rank),
            )
        ]
