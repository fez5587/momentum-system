"""Extraction (§8): local Ollama turns a transcript into DESCRIPTIVE claim rows. Never
evaluative — it records WHAT was claimed + an EXACT verbatim quote; it does not judge whether
the claim is correct or tradeable (all judgment lives downstream). A claim without a
verbatim_quote is dropped: that quote is the chain of custody the reasoning model verifies
against ASR error."""

import json
import re
import urllib.request

from youtube_claims import config


def _norm(s: str) -> str:
    """Collapse whitespace + lowercase so a verbatim quote still matches across the segment
    joins and the punctuation-poor text of auto-captions."""
    return re.sub(r"\s+", " ", s or "").strip().lower()


def _quote_supported(verbatim_quote: str, ntext: str) -> bool:
    """Anti-hallucination: is the quote actually present in the (already whitespace-normalized)
    source window? Matches a ~40-char prefix to tolerate minor ASR word-boundary noise while
    still rejecting a fabricated quote."""
    nvq = _norm(verbatim_quote)
    return bool(nvq) and (nvq[:40] in ntext or nvq in ntext)

_PROMPT = """You are a precise information extractor. From the TRANSCRIPT below, extract every
distinct market/trading CLAIM about a specific asset (stock, crypto, ETF, index, commodity, fx).

STRICT RULES:
- DESCRIPTIVE ONLY. Record what the speaker CLAIMED. Never judge if it is correct, good, or
  tradeable. `direction` describes the claim (bullish/bearish/neutral/mixed), not a recommendation.
- Every claim MUST include `verbatim_quote`: text copied EXACTLY from the transcript (word-for-word)
  that supports the claim. If you cannot copy an exact supporting quote, DO NOT emit the claim.
- `extraction_confidence` (0-1) is how sure you are you PARSED it correctly — NOT that it is true.
- Prefer claims about these watchlist assets if present, but include others too: {watchlist}
- If the transcript contains no asset-specific claims, return {{"claims": []}}.

Return ONLY JSON: {{"claims": [ {{
  "asset_ticker": "AAPL or null", "asset_name": "as spoken or null",
  "asset_class": "equity|crypto|etf|index|commodity|fx|other",
  "direction": "bullish|bearish|neutral|mixed",
  "claim_text": "short paraphrase of what was asserted",
  "verbatim_quote": "EXACT transcript text",
  "stated_rationale": "reason given or null", "stated_horizon": "e.g. next quarter or null",
  "extraction_confidence": 0.0
}} ] }}

TRANSCRIPT:
{transcript}
"""


def _ollama_generate(prompt: str, model: str, host: str) -> str:
    payload = {"model": model, "prompt": prompt, "stream": False, "format": "json",
               "options": {"temperature": 0.1, "num_predict": 2048}}
    req = urllib.request.Request(host.rstrip("/") + "/api/generate",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=240) as r:
        return json.load(r).get("response", "")


def _chunks(segments: list[dict], max_chars: int = 8000):
    """Group whisper segments into ~max_chars windows, carrying their time span so extracted
    claims can be time-located and their quotes checked against the exact window text."""
    buf, start, n = [], None, 0
    for seg in segments:
        t = (seg.get("text") or "").strip()
        if not t:
            continue
        if start is None:
            start = seg.get("start")
        buf.append((seg, t))
        n += len(t)
        if n >= max_chars:
            yield buf, start, seg.get("end")
            buf, start, n = [], None, 0
    if buf:
        yield buf, start, buf[-1][0].get("end")


def _normalize(c: dict, win_start, win_end) -> dict | None:
    vq = (c.get("verbatim_quote") or "").strip()
    if not vq:
        return None                      # mandatory — drop unsupported claims
    tkr = (c.get("asset_ticker") or "").strip().upper() or None
    return {
        "asset_ticker": tkr,
        "asset_name": (c.get("asset_name") or None),
        "asset_class": (c.get("asset_class") or None),
        "direction": (c.get("direction") or None),
        "claim_text": (c.get("claim_text") or None),
        "verbatim_quote": vq,
        "timestamp_start": win_start,
        "timestamp_end": win_end,
        "stated_rationale": (c.get("stated_rationale") or None),
        "stated_horizon": (c.get("stated_horizon") or None),
        "extraction_confidence": c.get("extraction_confidence"),
    }


def extract_claims(segments: list[dict], watchlist: list[str] | None = None,
                   *, model: str | None = None, host: str | None = None) -> list[dict]:
    """Extract descriptive claims from whisper segments. Chunks long transcripts so nothing is
    dropped, and attaches each claim's window time span. Pure w.r.t. the DB (caller inserts)."""
    model = model or config.OLLAMA_MODEL
    host = host or config.ollama_host()
    wl = ", ".join(watchlist if watchlist is not None else config.watchlist()) or "(none specified)"
    out: list[dict] = []
    for win, w_start, w_end in _chunks(segments):
        text = " ".join(t for _, t in win)
        ntext = _norm(text)
        try:
            raw = _ollama_generate(_PROMPT.format(watchlist=wl, transcript=text), model, host)
            data = json.loads(raw)
        except Exception:  # noqa: BLE001 — a bad chunk shouldn't kill the whole video
            continue
        for c in (data.get("claims") or []):
            # keep only claims whose quote is actually present in THIS window (anti-hallucination)
            if _quote_supported(c.get("verbatim_quote") or "", ntext):
                nc = _normalize(c, w_start, w_end)
                if nc:
                    out.append(nc)
    return out
