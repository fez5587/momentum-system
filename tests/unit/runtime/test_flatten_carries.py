"""Overnight-carry detection for the open catch-up flatten."""

from datetime import date

from runtime.flatten import buy_fills_from_orders, find_overnight_carries

TODAY = date(2026, 6, 22)


def _buy(sym, filled_at):
    return {"symbol": sym, "filled_at": filled_at}


def test_position_bought_today_is_not_a_carry():
    # 14:00Z == 10:00 ET on 6/22 -> bought today
    carries = find_overnight_carries({"DAMD"}, [_buy("DAMD", "2026-06-22T14:00:00Z")], TODAY)
    assert carries == []


def test_position_with_no_buy_today_is_a_carry():
    # only a prior-day buy -> carried overnight
    carries = find_overnight_carries({"ATPC"}, [_buy("ATPC", "2026-06-18T14:26:00Z")], TODAY)
    assert carries == ["ATPC"]


def test_mixed_book_returns_only_carries():
    open_syms = {"ATPC", "DAMD", "NIXX"}
    fills = [
        _buy("ATPC", "2026-06-18T14:26:00Z"),   # carry
        _buy("DAMD", "2026-06-22T14:34:00Z"),   # today
        _buy("NIXX", "2026-06-22T14:00:00Z"),   # today
    ]
    assert find_overnight_carries(open_syms, fills, TODAY) == ["ATPC"]


def test_late_utc_fill_maps_to_prior_et_day():
    # 01:00Z on 6/22 == 21:00 ET on 6/21 -> a prior session, so a carry
    carries = find_overnight_carries({"XYZ"}, [_buy("XYZ", "2026-06-22T01:00:00Z")], TODAY)
    assert carries == ["XYZ"]


def test_no_open_positions_no_carries():
    assert find_overnight_carries(set(), [_buy("AAA", "2026-06-18T14:00:00Z")], TODAY) == []


def test_open_with_no_fills_at_all_is_a_carry():
    # open but we have zero buy-fill records for it -> treat as carried (fail safe)
    assert find_overnight_carries({"GHOST"}, [], TODAY) == ["GHOST"]


# --- buy_fills_from_orders: which orders count as a real today-entry ----------
def test_partial_fill_then_canceled_buy_counts_as_a_fill():
    # SSPC-2026-06-24: marketable buy 88 filled 84, remainder canceled -> the
    # ORDER status reads 'canceled' but it opened 84 real shares. Must count.
    orders = [{"symbol": "SSPC", "side": "buy", "status": "canceled",
               "filled_qty": "84", "filled_at": "2026-06-24T13:38:54Z", "legs": None}]
    fills = buy_fills_from_orders(orders)
    assert [f["symbol"] for f in fills] == ["SSPC"]


def test_fully_filled_buy_counts():
    orders = [{"symbol": "NOK", "side": "buy", "status": "filled",
               "filled_qty": "1000", "filled_at": "2026-06-24T13:37:18Z"}]
    assert [f["symbol"] for f in buy_fills_from_orders(orders)] == ["NOK"]


def test_zero_fill_canceled_buy_is_not_a_fill():
    # a backed-out entry that never filled any shares -> NOT a today-entry
    orders = [{"symbol": "GRAB", "side": "buy", "status": "canceled",
               "filled_qty": "0", "filled_at": None}]
    assert buy_fills_from_orders(orders) == []


def test_partial_fill_position_is_not_flattened_as_a_carry():
    # end-to-end: the partial fill makes the position "bought today" -> NOT a carry
    fills = buy_fills_from_orders(
        [{"symbol": "SSPC", "side": "buy", "status": "canceled", "filled_qty": "84",
          "filled_at": "2026-06-24T13:38:54Z"}])
    assert find_overnight_carries({"SSPC"}, fills, date(2026, 6, 24)) == []
