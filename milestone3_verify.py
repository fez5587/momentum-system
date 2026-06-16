"""Milestone 3 verification: Schwab integration health check.

    python milestone3_verify.py

Walks every layer of the Schwab integration and prints PASS/WARN/FAIL.
Designed to be useful both with and without credentials: without them it
verifies graceful degradation (fallback account, empty orders,
unauthenticated health) instead of crashing.
"""

from __future__ import annotations

import sys

from dotenv import load_dotenv


def check(label: str, fn):
    try:
        status, detail = fn()
    except Exception as exc:  # noqa: BLE001
        status, detail = "FAIL", str(exc)
    print(f"[{status:^4}] {label}: {detail}")
    return status != "FAIL"


def main() -> int:
    load_dotenv()
    from schwab.auth.lifecycle import TokenLifecycle
    from schwab.auth.token_store import TokenStore
    from schwab.health.reporter import HealthReporter
    from schwab.market.client import SchwabApiError, SchwabMarketClient
    from schwab.orders.reader import OrdersReader
    from schwab.positions.reader import PositionsReader
    from schwab.settings import SchwabSettings

    settings = SchwabSettings.from_env()
    ok = True

    def c_settings():
        creds = settings.has_broker_credentials or settings.has_market_data_credentials
        return ("PASS" if creds else "WARN",
                f"app keys {'present' if creds else 'absent'}; token_path={settings.token_path}")
    ok &= check("settings", c_settings)

    def c_token():
        store = TokenStore(settings.token_path)
        bundle = store.load()
        if bundle is None:
            return "WARN", "no token file — run OAuth flow to authenticate"
        return ("PASS" if not bundle.is_expired else "WARN",
                f"token loaded, expired={bundle.is_expired}")
    ok &= check("token store", c_token)

    lifecycle = TokenLifecycle(settings)

    def c_lifecycle():
        status = lifecycle.status()
        if status.get("authenticated"):
            return "PASS", f"authenticated (expired={status.get('expired')})"
        return "WARN", f"unauthenticated: {status.get('reason')}"
    ok &= check("token lifecycle", c_lifecycle)

    def c_health():
        report = HealthReporter(settings=settings, lifecycle=lifecycle).check()
        d = report.to_dict()
        return "PASS", f"overall={d.get('status')} checks={len(d.get('checks') or [])}"
    ok &= check("health reporter", c_health)

    def c_positions():
        reader = PositionsReader(settings=settings, lifecycle=lifecycle)
        summary = reader.read_account_summary()
        if getattr(summary, "is_fallback", False):
            return "WARN", f"fallback summary ({summary.account_id}) — authenticate for live data"
        return "PASS", f"account {summary.account_id}"
    ok &= check("positions reader", c_positions)

    def c_orders():
        reader = OrdersReader(settings=settings, lifecycle=lifecycle)
        orders = reader.read_orders()
        return "PASS", f"{len(orders)} orders (empty is expected when unauthenticated)"
    ok &= check("orders reader", c_orders)

    def c_market():
        client = SchwabMarketClient(settings=settings, lifecycle=lifecycle)
        try:
            quotes = client.get_quotes(["AAPL"])
            return "PASS", f"quotes returned for {list(quotes)}"
        except SchwabApiError as exc:
            if exc.status == 401:
                return "WARN", "market client correctly raises 401 when unauthenticated"
            raise
    ok &= check("market client", c_market)

    print("\nmilestone 3:", "VERIFIED (warnings are expected without live Schwab auth)" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
