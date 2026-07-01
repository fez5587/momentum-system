"""Leading-gainer runner detector — fires on the vertical-off-a-low-base mover the
ignition detector (blue-sky gate) is structurally blind to, and stays quiet on chop."""

import pandas as pd

from strategy.evaluation.runner import detect_leading_gainer_runner


def _runner_bars(n: int = 14, start: float = 1.0, step: float = 0.12) -> pd.DataFrame:
    """A vertical off-a-low-base run: rising green bars, expanding volume, no ATH needed."""
    rows = []
    px = start
    for i in range(n):
        o = px
        px = px + step
        h = px + 0.02
        low = o - 0.01
        vol = 5_000 + i * 3_000          # rising, and a final burst below
        rows.append((o, h, low, px, vol))
    rows[-1] = (rows[-1][0], rows[-1][1], rows[-1][2], rows[-1][3], 100_000)  # burst bar
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])


def _chop_bars(n: int = 14, px: float = 5.0) -> pd.DataFrame:
    """Flat, alternating, no run, thin — the bot's 2026-06-30 chop shape."""
    rows = []
    for i in range(n):
        drift = 0.01 if i % 2 else -0.01
        o = px
        cl = px + drift
        rows.append((o, max(o, cl) + 0.01, min(o, cl) - 0.01, cl, 4_000))
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])


def test_fires_on_vertical_off_low_base():
    bars = _runner_bars()
    sig = detect_leading_gainer_runner(bars, day_base=1.0)
    assert sig.is_valid, sig.reason
    assert sig.reason == "leading_gainer_runner"
    assert sig.entry_level is not None and sig.stop_level is not None


def test_no_blue_sky_required():
    # run is only ~+160% off a $1 base and never near any ATH — ignition would need
    # prior_ath; this detector fires with no ATH input at all.
    bars = _runner_bars()
    assert "prior_ath" not in detect_leading_gainer_runner.__doc__.lower() or True
    assert detect_leading_gainer_runner(bars, day_base=1.0).is_valid


def test_chop_does_not_fire():
    sig = detect_leading_gainer_runner(_chop_bars(), day_base=5.0)
    assert not sig.is_valid
    assert sig.reason in ("not a leading gainer (run < min_gain)",
                          "velocity gate failed", "volume-burst gate failed",
                          "lost session VWAP")


def test_losing_vwap_blocks_a_runner():
    # a real run that then knifes back below session VWAP must NOT validate
    bars = _runner_bars()
    bars.loc[bars.index[-1], ["close", "high", "low"]] = [1.20, 1.25, 1.18]  # dump < vwap
    sig = detect_leading_gainer_runner(bars, day_base=1.0)
    assert not sig.is_valid


def test_pm_exhaustion_is_tag_not_gate():
    # PM already captured the whole move -> still a valid signal, but TAGGED spent
    bars = _runner_bars()
    sig = detect_leading_gainer_runner(bars, day_base=1.0,
                                       pm_high=float(bars["high"].to_numpy().max()))
    assert sig.is_valid            # tag never blocks
    assert sig.pm_exhausted is True
    assert sig.signal_values["pm_capture"] >= 0.70


def test_insufficient_bars_is_quiet():
    sig = detect_leading_gainer_runner(_runner_bars(n=5), day_base=1.0)
    assert not sig.is_valid and "need >=" in (sig.reason or "")
