"""Optional LLM (Ollama) enrichment hooks. Disabled unless configured."""

from __future__ import annotations

import json
import logging
import urllib.request

logger = logging.getLogger(__name__)


def summarize_setup_with_llm(
    setup: dict,
    host: str = "http://localhost:11434",
    model: str = "mistral",
    timeout: int = 15,
) -> str | None:
    """Ask a local Ollama model to summarize a setup. Returns None on failure."""
    prompt = (
        "Summarize this momentum trading setup in one sentence:\n"
        + json.dumps(setup, default=str)
    )
    try:
        req = urllib.request.Request(
            f"{host}/api/generate",
            data=json.dumps(
                {"model": model, "prompt": prompt, "stream": False}
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
        return body.get("response")
    except Exception:
        logger.debug("LLM enrichment unavailable", exc_info=True)
        return None
