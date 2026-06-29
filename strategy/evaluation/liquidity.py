"""Liquidity metrics: bid/ask spread.

Spread is a *quote* metric, not a *bar* metric — OHLCV bars carry no NBBO, which
is why ``minute_bars.spread_pct`` is null. The real, execution-relevant figure is
the spread at the moment a setup is evaluated: a wide book quietly eats R on the
thin sub-$20 names this system trades, so it is logged as a decision-time metric.
"""

from __future__ import annotations


def compute_spread_pct(bid: float | None, ask: float | None) -> float | None:
    """Relative bid/ask spread as a fraction of the mid price.

    Returns ``(ask - bid) / mid`` (e.g. ``0.01`` == a 1% spread) or ``None`` when
    the quote is missing or nonsensical (non-positive prices, or a crossed book
    where ``ask < bid``). ``None`` means "unknown" and must never collapse to
    ``0.0`` — a genuinely tight book and a missing quote are different facts a
    downstream liquidity gate has to tell apart. A locked book (``bid == ask``)
    is a real, valid ``0.0``.
    """
    if bid is None or ask is None:
        return None
    try:
        bid_f = float(bid)
        ask_f = float(ask)
    except (TypeError, ValueError):
        return None
    if bid_f <= 0 or ask_f <= 0 or ask_f < bid_f:
        return None
    mid = (bid_f + ask_f) / 2.0
    if mid <= 0:
        return None
    return (ask_f - bid_f) / mid
