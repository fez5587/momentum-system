"""Round-trip reconstruction from Alpaca order history (journal backfill)."""

from storage.journal_backfill import flatten_fills, reconstruct_round_trips


def _fill(sym, side, qty, price, time, otype="market", stop=None):
    return {"symbol": sym, "side": side, "qty": qty, "price": price,
            "time": time, "type": otype, "stop_price": stop}


def test_simple_long_round_trip():
    fills = [
        _fill("AAA", "buy", 10, 4.00, "2026-06-18T09:40:00Z"),
        _fill("AAA", "sell", 10, 4.50, "2026-06-18T10:15:00Z", "limit"),
    ]
    trips = reconstruct_round_trips(fills)
    assert len(trips) == 1
    t = trips[0]
    assert t["symbol"] == "AAA" and t["qty"] == 10
    assert t["entry_price"] == 4.0 and t["exit_price"] == 4.5
    assert t["realized_pnl"] == 5.0          # (4.50 - 4.00) * 10
    assert t["exit_reason"] == "take_profit" and t["side"] == "buy"


def test_two_round_trips_same_symbol_land_on_their_own_days():
    fills = [
        _fill("BBB", "buy", 10, 3.0, "2026-06-17T09:40:00Z"),
        _fill("BBB", "sell", 10, 2.7, "2026-06-17T11:00:00Z", "stop", stop=2.7),
        _fill("BBB", "buy", 10, 3.5, "2026-06-18T09:45:00Z"),
        _fill("BBB", "sell", 10, 3.8, "2026-06-18T10:30:00Z", "limit"),
    ]
    trips = reconstruct_round_trips(fills)
    assert [t["exit_time"][:10] for t in trips] == ["2026-06-17", "2026-06-18"]
    assert trips[0]["realized_pnl"] == -3.0 and trips[0]["exit_reason"] == "stop_loss"
    assert trips[0]["stop_loss_price"] == 2.7
    assert trips[1]["realized_pnl"] == 3.0


def test_open_position_is_not_a_round_trip():
    fills = [_fill("CCC", "buy", 10, 5.0, "2026-06-18T09:40:00Z")]   # never sold
    assert reconstruct_round_trips(fills) == []


def test_scale_out_one_trip_avg_exit():
    fills = [
        _fill("DDD", "buy", 10, 2.0, "2026-06-18T09:40:00Z"),
        _fill("DDD", "sell", 5, 2.4, "2026-06-18T10:00:00Z", "limit"),
        _fill("DDD", "sell", 5, 2.6, "2026-06-18T10:20:00Z", "limit"),
    ]
    trips = reconstruct_round_trips(fills)
    assert len(trips) == 1
    assert trips[0]["exit_price"] == 2.5            # (2.4+2.6)/2
    assert trips[0]["realized_pnl"] == 5.0          # (2.5-2.0)*10


def test_short_round_trip():
    fills = [
        _fill("EEE", "sell", 10, 5.0, "2026-06-18T09:40:00Z"),
        _fill("EEE", "buy", 10, 4.5, "2026-06-18T10:00:00Z"),
    ]
    trips = reconstruct_round_trips(fills)
    assert len(trips) == 1
    assert trips[0]["side"] == "sell"
    assert trips[0]["realized_pnl"] == 5.0          # short: (5.0-4.5)*10


def test_flatten_fills_includes_bracket_legs():
    raw = [{
        "id": "p1", "symbol": "FFF", "side": "buy", "status": "filled",
        "filled_qty": "10", "filled_avg_price": "4.0", "filled_at": "2026-06-18T09:40:00Z",
        "type": "market",
        "legs": [
            {"id": "l1", "side": "sell", "status": "filled", "filled_qty": "10",
             "filled_avg_price": "3.6", "filled_at": "2026-06-18T11:00:00Z",
             "type": "stop", "stop_price": "3.6"},
            {"id": "l2", "side": "sell", "status": "canceled", "type": "limit"},
        ],
    }]
    fills = flatten_fills(raw)
    assert len(fills) == 2                           # parent buy + filled stop leg (canceled leg dropped)
    assert fills[1]["symbol"] == "FFF" and fills[1]["stop_price"] == 3.6
    trips = reconstruct_round_trips(fills)
    assert trips[0]["realized_pnl"] == -4.0 and trips[0]["exit_reason"] == "stop_loss"
