"""Label-quality evaluation for the Ollama catalyst classifier.

Runs a fixed, hand-labeled set of representative small-cap headlines through
``classify_headline`` and scores the model's labels against the ground truth.
This is the objective basis for (a) confirming the few-shot / schema prompt
helps and (b) A/B-ing models (``qwen3:14b`` vs ``qwen2.5:7b-instruct``) on the
SAME inputs.

The scoring (`score_predictions`) is PURE — no Ollama, no DB — so it is unit
tested directly. ``run_eval`` is the thin live wrapper that calls the model.

The single most decision-relevant metric is **is_dilutive precision/recall**:
that flag is what the Phase-2 dilution veto acts on, so a false negative lets a
dilutive trap through and a false positive vetoes a good name.
"""

from __future__ import annotations

from research.ingestion.news_enrichment import CatalystAnalysis, classify_headline

# A representative, hand-labeled fixture spanning the catalyst vocabulary.
# ``sentiment_sign`` is the EXPECTED sign for the stock: +1 bullish, -1 bearish,
# 0 neutral/ambiguous. It is scored with a deadband so near-zero reads pass.
FIXTURE: list[dict] = [
    {"headline": "Acme Bio Announces $30 Million Registered Direct Offering Priced At-The-Market",
     "catalyst_type": "offering_dilution", "is_dilutive": True, "sentiment_sign": -1},
    {"headline": "NanoTech Prices Underwritten Public Offering of 8 Million Shares at $2.10",
     "catalyst_type": "offering_dilution", "is_dilutive": True, "sentiment_sign": -1},
    {"headline": "QuantumX Files Form S-3 Shelf Registration for Up to $200 Million",
     "catalyst_type": "offering_dilution", "is_dilutive": True, "sentiment_sign": -1},
    {"headline": "CureGen Receives FDA Approval for Its Lead Oncology Therapy",
     "catalyst_type": "fda_approval", "is_dilutive": False, "sentiment_sign": 1},
    {"headline": "TrialCo Reports Positive Topline Phase 3 Results Meeting Primary Endpoint",
     "catalyst_type": "clinical_trial", "is_dilutive": False, "sentiment_sign": 1},
    {"headline": "BioX Drug Fails to Meet Primary Endpoint in Phase 3 Study",
     "catalyst_type": "clinical_trial", "is_dilutive": False, "sentiment_sign": -1},
    {"headline": "RetailCo Reports Q4 Revenue Up 35%, Beating Analyst Estimates",
     "catalyst_type": "earnings", "is_dilutive": False, "sentiment_sign": 1},
    {"headline": "WidgetCorp Misses Q2 Earnings as Margins Compress",
     "catalyst_type": "earnings", "is_dilutive": False, "sentiment_sign": -1},
    {"headline": "TargetCo to Be Acquired by MegaCorp for $12.00 Per Share in Cash",
     "catalyst_type": "ma_acquisition", "is_dilutive": False, "sentiment_sign": 1},
    {"headline": "TechCo Signs Multi-Year Supply Agreement With a Major Automaker",
     "catalyst_type": "partnership_contract", "is_dilutive": False, "sentiment_sign": 1},
    {"headline": "SoftCo Raises Full-Year Revenue Guidance Above Consensus",
     "catalyst_type": "guidance_update", "is_dilutive": False, "sentiment_sign": 1},
    {"headline": "PharmaCo Receives FDA Complete Response Letter for Its Drug Application",
     "catalyst_type": "regulatory", "is_dilutive": False, "sentiment_sign": -1},
    {"headline": "MicroCap Announces 1-for-20 Reverse Stock Split to Regain Listing Compliance",
     "catalyst_type": "stock_split_reverse", "is_dilutive": False, "sentiment_sign": -1},
    {"headline": "GadgetCo Upgraded to Buy at a Major Bank, Price Target Raised",
     "catalyst_type": "analyst_rating", "is_dilutive": False, "sentiment_sign": 1},
    {"headline": "BoardCo Authorizes $100 Million Share Repurchase Program",
     "catalyst_type": "insider_buyback", "is_dilutive": False, "sentiment_sign": 1},
    {"headline": "CEO of HoldingCo to Present at an Upcoming Investor Conference",
     "catalyst_type": "none", "is_dilutive": False, "sentiment_sign": 0},
]


def _sign(value, deadband: float = 0.15) -> int:
    """-1 / 0 / +1 with a neutral deadband so near-zero reads count as neutral."""
    if value is None:
        return 0
    if value > deadband:
        return 1
    if value < -deadband:
        return -1
    return 0


def _field(pred, name):
    """Read a field off a CatalystAnalysis or a plain dict (keeps scoring pure)."""
    if pred is None:
        return None
    if isinstance(pred, dict):
        return pred.get(name)
    return getattr(pred, name, None)


def score_predictions(cases: list[tuple]) -> dict:
    """Score (expected_dict, predicted) pairs. PURE — no Ollama/DB.

    ``predicted`` may be a ``CatalystAnalysis``, a dict, or ``None`` (model
    failed → counted as an error, not scored). Returns accuracy for the
    catalyst type and sentiment sign, plus precision/recall/F1 for the
    is_dilutive flag (the veto's basis) and the list of type mistakes."""
    n = len(cases)
    type_hits = sent_hits = errors = scored = 0
    tp = fp = fn = tn = 0
    confusion: list[dict] = []
    for exp, pred in cases:
        if pred is None:
            errors += 1
            continue
        scored += 1
        p_type = _field(pred, "catalyst_type")
        if p_type == exp["catalyst_type"]:
            type_hits += 1
        else:
            confusion.append({
                "headline": exp.get("headline", ""),
                "expected": exp["catalyst_type"], "predicted": p_type,
            })
        e_dil, p_dil = bool(exp["is_dilutive"]), bool(_field(pred, "is_dilutive"))
        if p_dil and e_dil:
            tp += 1
        elif p_dil and not e_dil:
            fp += 1
        elif not p_dil and e_dil:
            fn += 1
        else:
            tn += 1
        if "sentiment_sign" in exp and _sign(_field(pred, "sentiment")) == exp["sentiment_sign"]:
            sent_hits += 1

    def _ratio(a, b):
        return round(a / b, 4) if b else None

    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)
    f1 = (round(2 * precision * recall / (precision + recall), 4)
          if precision and recall else None)
    return {
        "n": n, "scored": scored, "errors": errors,
        "type_accuracy": _ratio(type_hits, scored),
        "sentiment_sign_accuracy": _ratio(sent_hits, scored),
        "dilutive": {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
                     "precision": precision, "recall": recall, "f1": f1},
        "confusion": confusion,
    }


def run_eval(cases: list[dict] | None = None, **kwargs) -> tuple[dict, list[tuple]]:
    """Classify each fixture headline live, then score. Returns (metrics, pairs).

    ``kwargs`` pass straight through to ``classify_headline`` (host, model,
    timeout, use_schema, ...). A model failure on a case yields ``None`` for
    that prediction (counted as an error)."""
    cases = cases if cases is not None else FIXTURE
    pairs = [
        (c, classify_headline(c["headline"], c.get("snippet", ""),
                              c.get("tickers", ""), **kwargs))
        for c in cases
    ]
    return score_predictions(pairs), pairs


def format_report(metrics: dict, model: str = "") -> str:
    """Human-readable metrics block for the CLI."""
    d = metrics["dilutive"]
    def pct(x):
        return f"{x * 100:5.1f}%" if isinstance(x, (int, float)) else "  n/a"
    lines = [
        f"Catalyst label eval  —  model={model or '?'}",
        f"  cases: {metrics['n']}   scored: {metrics['scored']}   errors: {metrics['errors']}",
        f"  catalyst_type accuracy : {pct(metrics['type_accuracy'])}",
        f"  sentiment-sign accuracy: {pct(metrics['sentiment_sign_accuracy'])}",
        f"  is_dilutive  P={pct(d['precision'])} R={pct(d['recall'])} F1={pct(d['f1'])}"
        f"  (tp={d['tp']} fp={d['fp']} fn={d['fn']} tn={d['tn']})",
    ]
    if metrics["confusion"]:
        lines.append("  type misses:")
        for m in metrics["confusion"]:
            lines.append(f"    [{m['expected']} -> {m['predicted']}] {m['headline'][:70]}")
    return "\n".join(lines)
