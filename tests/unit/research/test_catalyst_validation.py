"""Tests for the catalyst→outcome validation harness (pure aggregation only)."""

import validate_catalysts as vc


def _row(otc, oth, has_cat, dil=None, score=None):
    return {"open_to_close_pct": otc, "open_to_high_pct": oth,
            "has_catalyst": has_cat, "is_dilutive": dil, "catalyst_score": score}


def test_outcome_correlation_counts_and_coverage():
    rows = [
        _row(-5.0, 2.0, True, dil=True, score=0.1),
        _row(8.0, 12.0, True, dil=False, score=0.8),
        _row(1.0, 3.0, False),  # no catalyst → excluded from splits
    ]
    s = vc.outcome_correlation(rows)
    assert s["total_gappers"] == 3
    assert s["with_catalyst"] == 2
    assert s["dilutive"]["n"] == 1
    assert s["non_dilutive"]["n"] == 1


def test_outcome_correlation_dilution_split_means():
    rows = [
        _row(-4.0, 1.0, True, dil=True),
        _row(-6.0, 1.0, True, dil=True),
        _row(10.0, 15.0, True, dil=False),
    ]
    s = vc.outcome_correlation(rows)
    assert s["dilutive"]["mean_open_to_close_pct"] == -5.0  # (-4 + -6)/2
    assert s["non_dilutive"]["mean_open_to_close_pct"] == 10.0


def test_outcome_correlation_score_buckets():
    rows = [
        _row(0.0, 1.0, True, dil=False, score=0.1),   # low
        _row(0.0, 1.0, True, dil=False, score=0.5),   # mid
        _row(0.0, 1.0, True, dil=False, score=0.9),   # high
        _row(0.0, 1.0, True, dil=False, score=None),  # none
    ]
    buckets = vc.outcome_correlation(rows)["by_score_bucket"]
    assert set(buckets) == {"low", "mid", "high", "none"}
    assert all(b["n"] == 1 for b in buckets.values())


def test_outcome_correlation_empty_when_no_catalysts():
    rows = [_row(3.0, 5.0, False), _row(-2.0, 1.0, False)]
    s = vc.outcome_correlation(rows)
    assert s["with_catalyst"] == 0
    assert s["dilutive"]["n"] == 0 and s["non_dilutive"]["n"] == 0
    # report renders the "run Phase 1 live first" guidance, no crash
    assert "run" in vc._format_report(s, n_sessions=2).lower()


def test_session_outcome_basic():
    import pandas as pd
    bars = pd.DataFrame({
        "open": [10.0, 11.0, 12.0],
        "high": [10.5, 13.0, 12.5],
        "low": [9.8, 10.9, 11.5],
        "close": [10.4, 12.0, 11.0],
    })
    out = vc._session_outcome(bars)
    assert out["open_to_close_pct"] == 10.0   # (11.0-10.0)/10.0*100
    assert out["open_to_high_pct"] == 30.0    # (13.0-10.0)/10.0*100


def test_session_outcome_empty():
    import pandas as pd
    out = vc._session_outcome(pd.DataFrame())
    assert out == {"open_to_close_pct": None, "open_to_high_pct": None}


def test_bucket_thresholds():
    assert vc._bucket(None) == "none"
    assert vc._bucket(0.2) == "low"
    assert vc._bucket(0.5) == "mid"
    assert vc._bucket(0.9) == "high"
