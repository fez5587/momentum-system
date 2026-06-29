"""AI trade-analysis (Ollama) tests.

The LLM call is mocked at ``urllib.request.urlopen`` (no network); the
persistence + dedupe tests run against an in-memory Postgres schema. Same
graceful-degradation contract as the news enrichment layer: any failure -> None,
never raises.
"""

import json
from datetime import date

import pytest

from strategy.evaluation import trade_analysis as ta
from storage.db import get_connection


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _reply(payload: dict) -> _FakeResp:
    return _FakeResp(json.dumps({"response": json.dumps(payload)}).encode())


def test_parse_happy_path():
    a = ta._parse(json.dumps({
        "decision": "pursue", "confidence": 0.8,
        "summary": "Clean FDA gap with volume", "concerns": ["thin float"],
    }))
    assert a.decision == "pursue" and a.confidence == 0.8
    assert a.summary == "Clean FDA gap with volume"
    assert a.concerns == ["thin float"]


def test_parse_validates_enum_and_clamps():
    a = ta._parse(json.dumps({"decision": "bogus", "confidence": 5, "summary": "x"}))
    assert a.decision == "none"        # unknown -> none
    assert a.confidence == 1.0         # clamped to [0,1]


def test_parse_concerns_string_coerced_to_list():
    a = ta._parse(json.dumps({"decision": "avoid", "confidence": 0.2,
                              "summary": "dilutive", "concerns": "offering risk"}))
    assert a.concerns == ["offering risk"]


def test_parse_malformed_returns_none():
    assert ta._parse("not json") is None
    assert ta._parse(json.dumps([1, 2, 3])) is None


def test_context_hash_stable_and_sensitive():
    h1 = ta.context_hash({"a": 1, "b": 2})
    h2 = ta.context_hash({"b": 2, "a": 1})   # key order irrelevant
    h3 = ta.context_hash({"a": 1, "b": 3})
    assert h1 == h2 and h1 != h3


# --------------------------------------------------------------------------
# prompt builders (pure)
# --------------------------------------------------------------------------

def test_prompt_builders_include_context_and_contract():
    ctx = {"symbol": "ABCD", "gap_pct": 22.0, "rvol": 8.0}
    for build in (ta.build_armed_prompt, ta.build_weak_prompt,
                  ta.build_postmortem_prompt, ta.build_eod_prompt):
        p = build(ctx)
        assert "ABCD" in p
        assert '"decision"' in p and '"concerns"' in p  # the JSON contract


def test_analyze_armed_setup_over_mock(monkeypatch):
    monkeypatch.setattr(ta.urllib.request, "urlopen",
                        lambda *a, **k: _reply({
                            "decision": "pursue", "confidence": 0.7,
                            "summary": "ok", "concerns": [],
                        }))
    a = ta.analyze_armed_setup({"symbol": "ABCD", "gap_pct": 20.0}, _Cfg())
    assert a is not None and a.decision == "pursue"


def test_call_network_down_returns_none(monkeypatch):
    def boom(*a, **k):
        raise OSError("refused")
    monkeypatch.setattr(ta.urllib.request, "urlopen", boom)
    assert ta.analyze_armed_setup({"symbol": "X"}, _Cfg()) is None


# --------------------------------------------------------------------------
# persistence + dedupe (in-memory Postgres)
# --------------------------------------------------------------------------

class _Cfg:
    host = "http://localhost:11434"
    model = "test-model"
    timeout_seconds = 5
    temperature = 0.3
    max_tokens = 256
    trade_analysis_batch_limit = 8


@pytest.fixture
def con():
    c = get_connection(":memory:")
    yield c
    c.close()


SESS = date(2026, 6, 29)


def test_run_trade_analysis_persists_and_dedupes(con, monkeypatch):
    snap = [
        {"symbol": "AAA", "state": "armed", "gap": 20.0, "rvol": 6.0,
         "trigger": 5.1, "stop": 4.8, "range_pct": 0.05, "dist": -0.01, "catalyst": "FDA nod"},
        {"symbol": "BBB", "state": "weak", "gap": 4.0, "rvol": 2.1,
         "trigger": 2.0, "stop": 1.95, "range_pct": 0.01, "dist": -0.2, "catalyst": ""},
        {"symbol": "CCC", "state": "waiting"},  # not analyzed
    ]
    monkeypatch.setattr(ta, "analyze_armed_setup",
                        lambda ctx, cfg: ta.TradeAnalysis("pursue", 0.8, "go", ["thin"]))
    monkeypatch.setattr(ta, "analyze_weak_setup",
                        lambda ctx, cfg: ta.TradeAnalysis("monitor", 0.4, "soft", []))

    res = ta.run_trade_analysis(con, snap, _Cfg(), SESS)
    assert res["analyzed"] == 2 and res["errors"] == 0

    rows = con.execute(
        "SELECT analysis_type, symbol, decision FROM ai_trade_analysis_cache "
        "ORDER BY symbol").fetchall()
    assert rows == [("armed", "AAA", "pursue"), ("weak", "BBB", "monitor")]

    # second pass, unchanged snapshot -> all skipped (context_hash)
    res2 = ta.run_trade_analysis(con, snap, _Cfg(), SESS)
    assert res2["analyzed"] == 0 and res2["skipped"] == 2


def test_run_trade_analysis_skips_when_llm_unavailable(con, monkeypatch):
    snap = [{"symbol": "AAA", "state": "armed", "gap": 20.0, "rvol": 6.0}]
    monkeypatch.setattr(ta, "analyze_armed_setup", lambda ctx, cfg: None)
    res = ta.run_trade_analysis(con, snap, _Cfg(), SESS)
    assert res["analyzed"] == 0 and res["errors"] == 1
    # cache not poisoned
    assert con.execute("SELECT count(*) FROM ai_trade_analysis_cache").fetchone()[0] == 0


def test_run_closed_trade_analysis_persists(con, monkeypatch):
    trades = [{"symbol": "AAA", "r_multiple": -1.0, "realized_pnl": -120.0,
               "exit_reason": "stop_loss"}]
    monkeypatch.setattr(ta, "analyze_closed_trade",
                        lambda ctx, cfg: ta.TradeAnalysis("none", 0.0, "stopped out",
                                                          ["entered extended"]))
    res = ta.run_closed_trade_analysis(con, trades, _Cfg(), SESS)
    assert res["analyzed"] == 1
    row = con.execute(
        "SELECT analysis_type, symbol, summary FROM ai_trade_analysis_cache").fetchone()
    assert row[0] == "postmortem" and row[1] == "AAA"


def test_run_session_narrative_writes_single_eod_row(con, monkeypatch):
    snap = [{"symbol": "AAA", "state": "armed"}]
    trades = [{"symbol": "AAA", "r_multiple": 1.5, "exit_reason": "profit_target"}]
    monkeypatch.setattr(ta, "analyze_session",
                        lambda ctx, cfg: ta.TradeAnalysis("none", 0.0, "solid day", ["press winners"]))
    res = ta.run_session_narrative(con, snap, trades, _Cfg(), SESS)
    assert res["analyzed"] == 1
    row = con.execute(
        "SELECT analysis_type, symbol, summary FROM ai_trade_analysis_cache "
        "WHERE analysis_type = 'eod'").fetchone()
    assert row[1] == "" and row[2] == "solid day"
    # unchanged -> skipped on a second pass
    assert ta.run_session_narrative(con, snap, trades, _Cfg(), SESS)["skipped"] == 1


def test_config_from_env_reads_trade_analysis_flags():
    from config import OllamaConfig
    cfg = OllamaConfig.from_env({
        "TRADE_ANALYSIS_ENABLED": "1",
        "TRADE_ANALYSIS_INTERVAL_SECONDS": "90",
        "TRADE_ANALYSIS_BATCH_LIMIT": "4",
    })
    assert cfg.trade_analysis_enabled is True
    assert cfg.trade_analysis_interval_seconds == 90
    assert cfg.trade_analysis_batch_limit == 4
    # defaults off
    assert OllamaConfig.from_env({}).trade_analysis_enabled is False


def test_trade_analysis_map_groups_by_type(con, monkeypatch):
    snap = [{"symbol": "AAA", "state": "armed", "gap": 20.0}]
    monkeypatch.setattr(ta, "analyze_armed_setup",
                        lambda ctx, cfg: ta.TradeAnalysis("avoid", 0.9, "dilutive", ["offering"]))
    ta.run_trade_analysis(con, snap, _Cfg(), SESS)
    m = ta.trade_analysis_map(con, SESS)
    assert "armed" in m and m["armed"]["AAA"]["decision"] == "avoid"
    assert m["armed"]["AAA"]["concerns"] == ["offering"]
