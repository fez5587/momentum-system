"""Safely flatten a position that has resting protective orders.

THE PROBLEM this solves (learned the hard way, twice, in live trading):
a bracket's stop-loss / take-profit leg RESERVES the position's full quantity at
the broker (``held_for_orders``). So a plain ``close_position`` is rejected with
HTTP 403 ``"insufficient qty available for order (available: 0)"`` — the close
silently fails and the position is left sitting there, often unprotected once its
DAY stop expires. This bit us in two places independently:

  1. the live exit manager's trail / first-red exits (positions never exited), and
  2. the end-of-day flatten (the whole book was left naked overnight).

THE FIX, in one place: cancel the symbol's resting sell legs FIRST (which frees
the reserved shares), THEN market-close — with a few retries so a cancel that
hasn't settled at the broker yet doesn't abort the close. Both the exit manager
and the execution service (EOD flatten + circuit breaker) call this, so the fix
can never again drift out of sync between them.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# Order states in which a sell order still RESERVES position quantity. While an
# order is in one of these states, that many shares show as ``held_for_orders``
# and are unavailable to a separate close. (A ``filled``/``canceled`` leg no
# longer reserves anything, so we must not try to cancel it.)
RESERVING_STATES = frozenset({
    "held", "new", "accepted", "pending_new",
    "accepted_for_bidding", "partially_filled",
})


def cancel_protective_and_close(client, symbol: str, orders=None,
                                retries: int = 4) -> None:
    """Cancel ``symbol``'s resting protective sell orders, then market-close it.

    Args:
        client: broker client exposing ``cancel_order``, ``close_position`` and
            (optionally) ``get_orders(status, nested, symbols)``.
        symbol: the position to flatten.
        orders: a pre-fetched *nested* order list to scan for legs to cancel. Pass
            this when the caller already has a fresh snapshot (the exit manager
            fetches one per pass) to avoid a redundant round-trip. If ``None`` and
            the client supports it, this fetches the symbol's open orders itself.
        retries: close attempts, spaced out so an in-flight cancel can settle.

    Raises:
        the last close exception if every attempt fails (callers collect, not
        crash — one bad symbol must not block flattening the rest of the book).
    """
    # 1) free the reserved shares: cancel the symbol's working sell legs
    if orders is None and hasattr(client, "get_orders"):
        try:
            orders = client.get_orders(status="open", nested=True, symbols=[symbol])
        except Exception:  # noqa: BLE001
            orders = None
    for parent in (orders or []):
        for o in [parent, *(parent.get("legs") or [])]:
            sym = o.get("symbol") or parent.get("symbol")
            if (sym == symbol and o.get("side") == "sell"
                    and o.get("status") in RESERVING_STATES and o.get("id")):
                try:
                    client.cancel_order(o["id"])
                except Exception as exc:  # noqa: BLE001
                    logger.warning("cancel protective %s failed: %s", symbol, exc)

    # 2) liquidate — retry so a not-yet-settled cancel doesn't leave it hanging
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            client.close_position(symbol)
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(0.3 * (attempt + 1))
    raise last_exc if last_exc else RuntimeError(f"flatten {symbol} failed")
