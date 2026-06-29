"""Tests for the catalyst label-quality eval.

``score_predictions`` is pure (no Ollama/DB); ``run_eval`` is exercised over a
mocked classifier so the harness logic is covered without a live model.
"""

from research.ingestion import catalyst_eval as ce
from research.ingestion.news_enrichment import CatalystAnalysis


def _exp(ctype, dil, sign, headline="h"):
    return {"headline": headline, "catalyst_type": ctype,
            "is_dilutive": dil, "sentiment_sign": sign}


def test_score_predictions_perfect():
    cases = [
        (_exp("fda_approval", False, 1), CatalystAnalysis("fda_approval", 0.9, 0.9, False)),
        (_exp("offering_dilution", True, -1),
         CatalystAnalysis("offering_dilution", -0.8, 0.9, True)),
    ]
    m = ce.score_predictions(cases)
    assert m["scored"] == 2 and m["errors"] == 0
    assert m["type_accuracy"] == 1.0
    assert m["sentiment_sign_accuracy"] == 1.0
    assert m["dilutive"]["precision"] == 1.0 and m["dilutive"]["recall"] == 1.0
    assert m["confusion"] == []


def test_score_predictions_counts_none_as_error_not_scored():
    cases = [
        (_exp("earnings", False, 1), None),
        (_exp("earnings", False, 1), CatalystAnalysis("earnings", 0.7, 0.7, False)),
    ]
    m = ce.score_predictions(cases)
    assert m["n"] == 2 and m["scored"] == 1 and m["errors"] == 1
    assert m["type_accuracy"] == 1.0  # only the scored one counts


def test_score_predictions_dilutive_precision_recall():
    cases = [
        # false negative: real dilution the model missed
        (_exp("offering_dilution", True, -1), CatalystAnalysis("other", 0.0, 0.3, False)),
        # false positive: model flagged dilution that isn't
        (_exp("earnings", False, 1), CatalystAnalysis("earnings", 0.5, 0.6, True)),
        # true positive
        (_exp("offering_dilution", True, -1),
         CatalystAnalysis("offering_dilution", -0.7, 0.8, True)),
        # true negative
        (_exp("fda_approval", False, 1), CatalystAnalysis("fda_approval", 0.9, 0.9, False)),
    ]
    d = ce.score_predictions(cases)["dilutive"]
    assert (d["tp"], d["fp"], d["fn"], d["tn"]) == (1, 1, 1, 1)
    assert d["precision"] == 0.5 and d["recall"] == 0.5


def test_score_predictions_records_type_misses():
    cases = [(_exp("guidance_update", False, 1, "Raises guidance"),
              CatalystAnalysis("earnings", 0.4, 0.5, False))]
    m = ce.score_predictions(cases)
    assert m["type_accuracy"] == 0.0
    assert m["confusion"][0]["expected"] == "guidance_update"
    assert m["confusion"][0]["predicted"] == "earnings"


def test_sentiment_sign_deadband():
    # predicted sentiment within the deadband counts as neutral
    cases = [(_exp("none", False, 0), CatalystAnalysis("none", 0.05, 0.1, False))]
    assert ce.score_predictions(cases)["sentiment_sign_accuracy"] == 1.0


def test_run_eval_over_mocked_classifier(monkeypatch):
    # a "perfect" model that echoes each fixture's expected labels
    def fake_classify(headline, snippet="", tickers="", **kwargs):
        for c in ce.FIXTURE:
            if c["headline"] == headline:
                sign = c["sentiment_sign"]
                return CatalystAnalysis(
                    catalyst_type=c["catalyst_type"],
                    sentiment=0.8 * sign, conviction=0.8,
                    is_dilutive=c["is_dilutive"],
                )
        return None

    monkeypatch.setattr(ce, "classify_headline", fake_classify)
    metrics, pairs = ce.run_eval()
    assert len(pairs) == len(ce.FIXTURE)
    assert metrics["errors"] == 0
    assert metrics["type_accuracy"] == 1.0
    assert metrics["dilutive"]["precision"] == 1.0
    assert metrics["dilutive"]["recall"] == 1.0


def test_format_report_runs():
    cases = [(_exp("earnings", False, 1), CatalystAnalysis("earnings", 0.6, 0.6, False))]
    out = ce.format_report(ce.score_predictions(cases), model="m")
    assert "catalyst_type accuracy" in out and "is_dilutive" in out
