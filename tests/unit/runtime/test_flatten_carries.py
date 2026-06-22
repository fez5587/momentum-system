"""Overnight-carry detection for the open catch-up flatten."""

from datetime import date

from runtime.flatten import find_overnight_carries

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
