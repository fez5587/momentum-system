"""Trade-exit management — ONE source of truth for the backtest and live.

The original exit was a static bracket: a fixed stop at the opening-range low
(-1R) and a fixed take-profit at +target_r. Once filled, nothing moved, so a
trade could run to +1.8R and round-trip to a full loss, and losers were always
taken at the full stop.

This module adds active management as pure, parameterised logic so the exact
same rules drive the backtest (where we sweep them) and the live manager (where
we apply the winner). Rules, each independently toggleable:

  * breakeven  — once price reaches +breakeven_at_r, move the stop to entry
  * trail      — after +trail_after_r, trail the stop under each bar's low
                 (prior_low) or a % off the high-water mark (pct)
  * scale-out  — sell scale_out_pct of the position at +scale_out_r, run the rest
  * first-red  — exit on the first bar that closes below the prior bar's low
  * target_r   — final take-profit (whatever remains)

Intrabar convention is conservative: within a bar the STOP is assumed to fill
before the target/scale, so the backtest never flatters a trade.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import pandas as pd

TRAIL_NONE = "none"
TRAIL_PRIOR_LOW = "prior_low"
TRAIL_PCT = "pct"


def parse_profit_tiers(raw: str) -> list[tuple[float, float]]:
    """Parse 'gain:lock' percentage pairs, e.g. '8:3,15:9' -> [(.08,.03),(.15,.09)].

    Each pair means: once price has been up >= gain%, never let the trade close
    for less than lock% of profit (the stop ratchets to entry*(1+lock%)).
    """
    tiers: list[tuple[float, float]] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if ":" not in part:
            continue
        g, lock = part.split(":", 1)
        try:
            tiers.append((float(g) / 100.0, float(lock) / 100.0))
        except ValueError:
            continue
    return sorted(tiers)  # ascending by gain trigger


@dataclass
class ExitConfig:
    target_r: float = 2.0          # final take-profit in R (0 = no fixed target)
    breakeven_at_r: float = 0.0    # move stop to entry once +this R is reached (0 = off)
    # move stop to entry once price is +this FRACTION above entry (0 = off). A
    # percentage breakeven that fires once the breakout confirms — sooner than
    # +1R for a wide opening-range stop — so a winner can't round-trip to a loss
    # (the documented failure mode: small-cap pops then reverses through entry).
    breakeven_at_pct: float = 0.0
    trail_mode: str = TRAIL_NONE   # none | prior_low | pct
    trail_pct: float = 0.0         # pct trail: stop = high_water * (1 - trail_pct)
    trail_after_r: float = 1.0     # only start trailing once +this R is reached
    # STEP-TRAIL from the buy price: once the high-water reaches +N*step R, ratchet
    # the stop up to +(N-1)*step R — at +0.25R the stop sits at entry (breakeven),
    # at +0.5R at +0.25R, at +0.75R at +0.5R, ... Locks a small pop from
    # round-tripping AND climbs under a runner. Starts from entry, independent of
    # trail_after_r / trail_mode. 0 = off.
    trail_r_step: float = 0.0
    scale_out_r: float = 0.0       # sell scale_out_pct at +this R (0 = off)
    scale_out_pct: float = 0.5     # fraction sold at scale_out_r
    first_red_exit: bool = False   # exit on first close below the prior bar's low
    # percentage profit-lock checkpoints: [(gain_frac, lock_frac), ...]. Once the
    # trade has been up gain%, the stop never sits below entry*(1+lock%) — so a
    # winner can only give back down to a locked-in minimum gain.
    profit_lock_tiers: list = field(default_factory=list)
    # CATASTROPHE STOP — a hard market-exit backstop that does NOT depend on the
    # broker bracket stop being live. Fires when price is >= catastrophe_pct below
    # entry, OR (when a stop is known) >= catastrophe_risk_mult x the intended 1R
    # underwater. Catches a missing/failed protective stop (NIVF ran to -23% naked,
    # = the entire net loss). 0 = off.
    catastrophe_pct: float = 0.10
    catastrophe_risk_mult: float = 1.5
    # STOP ENFORCEMENT: a held position MUST have a live protective stop. If a
    # bracket's stop leg never attached or was stripped (the position is naked),
    # flatten it after this many consecutive naked passes (a grace so a just-
    # filled bracket's legs can attach first). 0 = off. The preventive complement
    # to the catastrophe stop (which is the -X% backstop).
    enforce_stop_grace_passes: int = 2
    # When a held position is first seen already protected at BREAKEVEN+ but its
    # original R is lost (the bracket stop was stripped or invalid — e.g. a fill below
    # the ORB-low left the stop > entry), there is no real risk distance to trail from,
    # so the stop would sit frozen at breakeven. Trail it on a SYNTHETIC R of this
    # fraction of entry instead, so the step-trail / profit-lock ratchet it UP as the
    # name rises (only ever up, never below the live breakeven stop). 0 = leave frozen.
    default_trail_r_pct: float = 0.10

    @classmethod
    def from_env(cls, env: dict | None = None) -> "ExitConfig":
        v = dict(os.environ)
        if env:
            v.update(env)

        def f(key, default):
            try:
                return float(v.get(key, default))
            except (TypeError, ValueError):
                return float(default)

        def b(key, default):
            return v.get(key, default).strip().lower() in {"1", "true", "yes", "on"}

        return cls(
            target_r=f("TRADING_REWARD_MULTIPLE", "2.0"),
            breakeven_at_r=f("TRADING_EXIT_BREAKEVEN_R", "0.0"),
            breakeven_at_pct=f("TRADING_EXIT_BREAKEVEN_PCT", "0.05"),
            trail_mode=v.get("TRADING_EXIT_TRAIL_MODE", TRAIL_NONE).strip().lower(),
            trail_pct=f("TRADING_EXIT_TRAIL_PCT", "0.0"),
            trail_after_r=f("TRADING_EXIT_TRAIL_AFTER_R", "1.0"),
            trail_r_step=f("TRADING_EXIT_TRAIL_R_STEP", "0.0"),
            scale_out_r=f("TRADING_EXIT_SCALE_OUT_R", "0.0"),
            scale_out_pct=f("TRADING_EXIT_SCALE_OUT_PCT", "0.5"),
            first_red_exit=b("TRADING_EXIT_FIRST_RED", "0"),
            profit_lock_tiers=parse_profit_tiers(v.get("TRADING_EXIT_PROFIT_TIERS", "")),
            catastrophe_pct=f("TRADING_CATASTROPHE_STOP_PCT", "0.10"),
            catastrophe_risk_mult=f("TRADING_CATASTROPHE_RISK_MULT", "1.5"),
            enforce_stop_grace_passes=int(f("TRADING_ENFORCE_STOP_GRACE_PASSES", "2")),
            default_trail_r_pct=f("TRADING_EXIT_DEFAULT_TRAIL_R_PCT", "0.10"),
        )

    def describe(self) -> str:
        parts = [f"target {self.target_r:g}R"]
        if self.breakeven_at_r:
            parts.append(f"BE@{self.breakeven_at_r:g}R")
        if self.breakeven_at_pct:
            parts.append(f"BE@+{self.breakeven_at_pct:.0%}")
        if self.trail_mode != TRAIL_NONE:
            how = f"{self.trail_pct:.1%}" if self.trail_mode == TRAIL_PCT else "prior-low"
            parts.append(f"trail {how} after {self.trail_after_r:g}R")
        if self.trail_r_step:
            parts.append(f"step-trail {self.trail_r_step:g}R")
        if self.scale_out_r:
            parts.append(f"scale {self.scale_out_pct:.0%}@{self.scale_out_r:g}R")
        if self.first_red_exit:
            parts.append("first-red")
        if self.profit_lock_tiers:
            parts.append("lock " + "/".join(
                f"+{g * 100:g}%->{lk * 100:g}%" for g, lk in self.profit_lock_tiers))
        if self.catastrophe_pct:
            parts.append(f"catastrophe@-{self.catastrophe_pct:.0%}/{self.catastrophe_risk_mult:g}xR")
        if self.enforce_stop_grace_passes:
            parts.append(f"enforce-stop({self.enforce_stop_grace_passes}p)")
        return ", ".join(parts)


def catastrophe_triggered(entry: float, current: float, stop: float | None,
                          pct: float, risk_mult: float) -> bool:
    """True if a LONG position is catastrophically underwater and must be
    market-exited NOW regardless of the broker bracket — the hard backstop for a
    missing or non-firing protective stop. Fires when price is >= ``pct`` below
    entry, OR (when a real stop is known) >= ``risk_mult`` x the intended 1R
    risk underwater. ``pct`` <= 0 disables the percentage arm."""
    if not entry or not current or current <= 0 or entry <= 0:
        return False
    loss_frac = (entry - current) / entry           # >0 = underwater (long)
    if pct and pct > 0 and loss_frac >= pct:
        return True
    if stop is not None and stop < entry and risk_mult and risk_mult > 0:
        risk = entry - stop
        if risk > 0 and current <= entry - risk_mult * risk:
            return True
    return False


@dataclass
class ExitFill:
    frac: float   # fraction of the original position closed by this fill
    price: float
    reason: str


@dataclass
class ExitResult:
    r_multiple: float          # realized R, weighted across partial fills
    reason: str                # reason of the FINAL (position-closing) fill
    exit_index: int            # index in bars_after where the position fully closed
    fills: list[ExitFill] = field(default_factory=list)


def _trail_stop(stop: float, entry: float, high_water: float,
                bar_low: float, reached_r: float, cfg: ExitConfig) -> float:
    """The stop after processing one bar — only ever ratchets UP."""
    new = stop
    if cfg.breakeven_at_r and reached_r >= cfg.breakeven_at_r:
        new = max(new, entry)
    # percentage breakeven: once the move clears +breakeven_at_pct above entry,
    # the trade can't go red (stop to entry). Fires off the high-water mark, so
    # it latches once reached and only ratchets the stop up, never down.
    if cfg.breakeven_at_pct and entry > 0 and high_water >= entry * (1.0 + cfg.breakeven_at_pct):
        new = max(new, entry)
    if reached_r >= cfg.trail_after_r:
        if cfg.trail_mode == TRAIL_PRIOR_LOW:
            new = max(new, bar_low)
        elif cfg.trail_mode == TRAIL_PCT and cfg.trail_pct > 0:
            new = max(new, high_water * (1.0 - cfg.trail_pct))
    # step-trail from the buy price: once the high-water clears +step R, lock the
    # stop one step behind (at +0.25R -> breakeven, +0.5R -> +0.25R, ...). risk is
    # recovered from reached_r so this needs no extra args. Only ratchets up (max).
    if cfg.trail_r_step and cfg.trail_r_step > 0 and reached_r >= cfg.trail_r_step:
        steps = int(reached_r / cfg.trail_r_step)        # floor; >= 1 inside this guard
        locked_r = (steps - 1) * cfg.trail_r_step
        risk = (high_water - entry) / reached_r if reached_r else 0.0
        new = max(new, entry + locked_r * risk)
    # percentage profit-lock checkpoints: once the trade has been up gain%, pin
    # the stop to at least entry*(1+lock%) so a winner keeps a minimum gain.
    if cfg.profit_lock_tiers and entry > 0:
        gain = (high_water - entry) / entry
        for gain_pct, lock_pct in cfg.profit_lock_tiers:
            if gain >= gain_pct:
                new = max(new, entry * (1.0 + lock_pct))
    return new


def simulate_exit(
    entry_price: float, init_stop: float, bars_after: pd.DataFrame, cfg: ExitConfig
) -> ExitResult:
    """Walk the bars AFTER entry and return the realized R for the whole trade.

    ``bars_after`` are the OHLC bars strictly after the entry bar, in order.
    The same per-bar rules are used live (see ``manage_live``).
    """
    risk = entry_price - init_stop
    if risk <= 0 or bars_after is None or len(bars_after) == 0:
        return ExitResult(0.0, "invalid", 0, [])

    target = entry_price + cfg.target_r * risk if cfg.target_r else None
    stop = init_stop
    high_water = entry_price
    remaining = 1.0
    realized_r = 0.0
    scaled = False
    fills: list[ExitFill] = []
    prev_low: float | None = None

    n = len(bars_after)
    for idx in range(n):
        bar = bars_after.iloc[idx]
        high = float(bar["high"]); low = float(bar["low"]); close = float(bar["close"])
        high_water = max(high_water, high)
        reached_r = (high_water - entry_price) / risk

        # (1) stop first (conservative intrabar ordering)
        if low <= stop:
            r = (stop - entry_price) / risk
            realized_r += remaining * r
            reason = "breakeven" if abs(stop - entry_price) < 1e-9 else (
                "trail_stop" if stop > init_stop else "stop_loss")
            fills.append(ExitFill(remaining, stop, reason))
            return ExitResult(realized_r, reason, idx, fills)

        # (2) scale-out into strength
        if (cfg.scale_out_r and not scaled
                and high >= entry_price + cfg.scale_out_r * risk):
            frac = min(cfg.scale_out_pct, remaining)
            realized_r += frac * cfg.scale_out_r
            remaining -= frac
            scaled = True
            fills.append(ExitFill(frac, entry_price + cfg.scale_out_r * risk, "scale_out"))
            if remaining <= 1e-9:
                return ExitResult(realized_r, "scale_out", idx, fills)

        # (3) final target on the remainder
        if target is not None and high >= target:
            realized_r += remaining * cfg.target_r
            fills.append(ExitFill(remaining, target, "target"))
            return ExitResult(realized_r, "target", idx, fills)

        # (4) first red candle that breaks the prior bar's low
        if cfg.first_red_exit and prev_low is not None and close < prev_low:
            r = (close - entry_price) / risk
            realized_r += remaining * r
            fills.append(ExitFill(remaining, close, "first_red"))
            return ExitResult(realized_r, "first_red", idx, fills)

        # (5) ratchet the stop for the NEXT bar
        stop = _trail_stop(stop, entry_price, high_water, low, reached_r, cfg)
        prev_low = low

    # session end on the remainder
    last_close = float(bars_after.iloc[-1]["close"])
    realized_r += remaining * ((last_close - entry_price) / risk)
    fills.append(ExitFill(remaining, last_close, "session_end"))
    return ExitResult(realized_r, "session_end", n - 1, fills)


@dataclass
class ExitDecision:
    desired_stop: float      # where the protective stop SHOULD be now
    scale_out_frac: float    # fraction to sell at market now (0 if none)
    exit_now: bool           # full market exit now (first-red)
    reason: str


def manage_live(
    entry_price: float, init_stop: float, bars_since_entry: pd.DataFrame,
    cfg: ExitConfig, already_scaled: bool = False,
) -> ExitDecision:
    """Compute the management action for an OPEN live position.

    Uses the same ratchet/scale/first-red rules as ``simulate_exit`` so the live
    behaviour matches the backtest. ``bars_since_entry`` are the closed bars from
    the entry bar onward. Returns the stop the broker order should be moved to
    (only ever up), whether to scale out now, and whether to exit outright.
    """
    risk = entry_price - init_stop
    if risk <= 0 or bars_since_entry is None or len(bars_since_entry) == 0:
        return ExitDecision(init_stop, 0.0, False, "hold")

    stop = init_stop
    high_water = entry_price
    for idx in range(len(bars_since_entry)):
        bar = bars_since_entry.iloc[idx]
        high_water = max(high_water, float(bar["high"]))
        reached_r = (high_water - entry_price) / risk
        stop = _trail_stop(stop, entry_price, high_water, float(bar["low"]),
                           reached_r, cfg)

    last = bars_since_entry.iloc[-1]
    last_close = float(last["close"])
    reached_r = (high_water - entry_price) / risk

    # first-red: latest bar closed below the prior bar's low
    if cfg.first_red_exit and len(bars_since_entry) >= 2:
        prior_low = float(bars_since_entry.iloc[-2]["low"])
        if last_close < prior_low and reached_r >= cfg.trail_after_r:
            return ExitDecision(stop, 0.0, True, "first_red")

    # scale-out: high reached the scale level and we haven't yet
    scale_frac = 0.0
    if (cfg.scale_out_r and not already_scaled
            and high_water >= entry_price + cfg.scale_out_r * risk):
        scale_frac = cfg.scale_out_pct

    return ExitDecision(stop, scale_frac, False, "trail")
