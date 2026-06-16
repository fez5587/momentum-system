"""Schwab integration tests (Milestone 3): token store, lifecycle, fallbacks."""

import json
import time

import pytest

from schwab.auth.lifecycle import TokenLifecycle
from schwab.auth.token_store import TokenBundle, TokenStore
from schwab.health.models import HealthStatus
from schwab.health.reporter import HealthReporter
from schwab.market.client import SchwabApiError, SchwabMarketClient
from schwab.orders.reader import OrdersReader
from schwab.positions.reader import PositionsReader
from schwab.settings import SchwabSettings
from storage.event_store import EventStore


@pytest.fixture
def settings(tmp_path):
    return SchwabSettings(token_path=str(tmp_path / "tokens.json"))


def make_bundle(expires_in=3600, refresh="refresh-tok"):
    return TokenBundle(
        access_token="access-tok",
        refresh_token=refresh,
        expires_at=time.time() + expires_in,
        token_type="Bearer",
        scope="api",
    )


def test_token_store_round_trip_and_permissions(settings):
    store = TokenStore(settings.token_path)
    assert store.load() is None
    store.save(make_bundle())
    loaded = store.load()
    assert loaded is not None
    assert loaded.access_token == "access-tok"
    assert not loaded.is_expired
    import os

    # The token file is locked to owner-only (0o600) on POSIX so OAuth tokens
    # are never world-readable. Windows does not implement POSIX permission
    # bits, so os.chmod can't produce 0o600 there — the round-trip above is the
    # meaningful check on that platform.
    if os.name != "nt":
        mode = os.stat(settings.token_path).st_mode & 0o777
        assert mode == 0o600  # tokens must never be world-readable


def test_token_bundle_expiry():
    assert make_bundle(expires_in=-10).is_expired
    assert not make_bundle(expires_in=600).is_expired


def test_lifecycle_status_unauthenticated(settings):
    lifecycle = TokenLifecycle(settings)
    status = lifecycle.status()
    assert status["authenticated"] is False
    assert lifecycle.get_access_token() is None


def test_lifecycle_status_authenticated(settings):
    TokenStore(settings.token_path).save(make_bundle())
    lifecycle = TokenLifecycle(settings)
    status = lifecycle.status()
    assert status["authenticated"] is True
    assert status["expired"] is False
    assert lifecycle.get_access_token() == "access-tok"


def test_positions_reader_falls_back_when_unauthenticated(settings):
    reader = PositionsReader(settings=settings, lifecycle=TokenLifecycle(settings))
    summary = reader.read_account_summary()
    assert summary.is_fallback
    assert summary.account_id == "SCHWAB-UNAUTH"
    positions = reader.read_positions()
    assert positions.is_fallback
    assert positions.positions == []


def test_orders_reader_empty_when_unauthenticated(settings):
    reader = OrdersReader(settings=settings, lifecycle=TokenLifecycle(settings))
    assert reader.read_orders() == []


def test_market_client_raises_401_without_token(settings):
    client = SchwabMarketClient(settings=settings, lifecycle=TokenLifecycle(settings))
    with pytest.raises(SchwabApiError) as exc_info:
        client.get_quotes(["AAPL"])
    assert exc_info.value.status == 401


def test_health_reporter_unauthenticated_and_event_emission(settings):
    store = EventStore(":memory:")
    reporter = HealthReporter(
        store=store, settings=settings,
        lifecycle=TokenLifecycle(settings), session_id="t",
    )
    report = reporter.check()
    d = report.to_dict()
    assert d["status"] in {
        HealthStatus.DOWN.value, HealthStatus.UNAUTHENTICATED.value, "down", "unauthenticated",
    }
    events = store.query_events(event_type="broker_health_changed")
    assert events, "first check must emit a broker_health_changed event"
    payload = json.loads(events[0]["payload_json"])
    assert payload["broker_name"] == "schwab"
    assert payload["new_health"]
    # second check with no change should not re-emit
    reporter.check()
    assert len(store.query_events(event_type="broker_health_changed")) == 1
    store.close()


def test_health_reporter_recovers_with_token(tmp_path):
    settings = SchwabSettings(
        token_path=str(tmp_path / "tokens.json"),
        broker_app_key="key", broker_app_secret="secret",
    )
    store = EventStore(":memory:")
    lifecycle = TokenLifecycle(settings)
    reporter = HealthReporter(
        store=store, settings=settings, lifecycle=lifecycle, session_id="t"
    )
    reporter.check()
    TokenStore(settings.token_path).save(make_bundle())
    second = reporter.check()
    events = store.query_events(event_type="broker_health_changed")
    assert len(events) == 2  # status changed -> new event
    payload = json.loads(events[-1]["payload_json"])
    assert payload["previous_health"] != payload["new_health"]
    store.close()
