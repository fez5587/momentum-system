"""Accumulate-then-re-validate the premarket (pm1) shadow track.

The pre-registered verdict on 2026-07-01 was PROMISING-BUT-UNDERPOWERED: the forward moves are
REAL (avg +11-14% upside, unlike the RTH levers' tight-stop artifacts) but n=21 is far too small
for the +1R Wilson bound to conclude. The live loop ingests premarket bars every trading day, so
this job rebuilds pm1 from the (growing) DB and appends a timestamped metrics row to a history
file. Run it on a schedule (systemd timer) or by hand; watch `n` climb and the Wilson LB tighten
until the locked bar is decisively cleared or failed.

  python -m research.premarket_eval
"""

import os
from datetime import datetime, timezone

from dotenv import load_dotenv

from research import labeler
from research.multi_schema import open_research_db

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

HIST = os.path.join(os.path.dirname(__file__), "..", "data", "premarket_eval_history.tsv")
_HEADER = "utc_ts\tn\tdays\trate_1r\twilson_lb\tmedian_up_60m\tmedian_dd_60m\tbig_gap_1r\tpassed\n"


def _metrics(con) -> dict:
    rows = con.execute(
        "SELECT l.max_upside_next_60m, l.max_drawdown_next_60m, "
        "CASE WHEN l.reached_1r_before_minus_1r THEN 1 ELSE 0 END, se.gap_pct "
        "FROM outcome_labels l JOIN setup_events se ON se.setup_id = l.setup_id "
        "WHERE se.setup_version=?", [labeler.PREMARKET_SETUP_VERSION]).fetchall()
    n = len(rows)
    days = con.execute("SELECT count(DISTINCT session_date) FROM setup_events WHERE setup_version=?",
                       [labeler.PREMARKET_SETUP_VERSION]).fetchone()[0]
    if not n:
        return {"n": 0, "days": days, "rate_1r": 0.0, "wilson_lb": 0.0,
                "median_up_60m": 0.0, "median_dd_60m": 0.0, "big_gap_1r": 0.0, "passed": False}
    r1 = [r[2] for r in rows]
    big = [r[2] for r in rows if (r[3] or 0) >= 0.5]
    med_up = labeler._median([r[0] for r in rows]) or 0.0
    med_dd = labeler._median([r[1] for r in rows]) or 0.0
    rate_1r = sum(r1) / n
    rate_big = (sum(big) / len(big)) if big else 0.0
    passed = (med_up >= labeler.PREMARKET_PROMO_MEDIAN_FWDMAX
              and med_dd > labeler.PREMARKET_PROMO_MEDIAN_ADVERSE
              and rate_1r > labeler.PREMARKET_PROMO_MIN_1R
              and rate_big > labeler.PREMARKET_PROMO_MIN_1R)
    return {"n": n, "days": days, "rate_1r": rate_1r,
            "wilson_lb": labeler._wilson_lower(sum(r1), n),
            "median_up_60m": med_up, "median_dd_60m": med_dd,
            "big_gap_1r": rate_big, "passed": passed}


def main() -> None:
    con = open_research_db("market")
    labeler.build_premarket(con, rebuild=True)          # rebuild from the grown DB
    m = _metrics(con)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    row = (f"{ts}\t{m['n']}\t{m['days']}\t{m['rate_1r']:.3f}\t{m['wilson_lb']:.3f}\t"
           f"{m['median_up_60m']:.4f}\t{m['median_dd_60m']:.4f}\t{m['big_gap_1r']:.3f}\t{m['passed']}")
    new = not os.path.exists(HIST)
    with open(HIST, "a") as f:
        if new:
            f.write(_HEADER)
        f.write(row + "\n")
    print("appended:", row)
    labeler.validate_premarket(con)


if __name__ == "__main__":
    main()
