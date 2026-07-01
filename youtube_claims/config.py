"""Config — env-driven (§10 defaults). Reuses the app's DATABASE_URL and Ollama host;
secrets (YOUTUBE_API_KEY) come from .env only, never hard-coded."""

import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# load .env once (the same file the trading app uses; gitignored)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, ".env"))
except Exception:  # noqa: BLE001
    pass

# --- §10 defaults (env-overridable) ---
POLL_INTERVAL_MINUTES = int(os.getenv("YT_POLL_INTERVAL_MINUTES", "30"))
PLAYLIST_PAGE_SIZE = 50
WORKER_CONCURRENCY = 1                      # deliberate — §13 (yt-dlp politeness)
INTER_VIDEO_DELAY_SECONDS = int(os.getenv("YT_INTER_VIDEO_DELAY_SECONDS", "60"))
MAX_RETRIES = int(os.getenv("YT_MAX_RETRIES", "3"))
RETRY_BACKOFF_BASE_SEC = 30                 # 30, 60, 120...
YT_DLP_FORMAT = "bestaudio"
YOUTUBE_API_QUOTA_DAILY = 10000
TRANSCRIPT_DIR = os.getenv("YT_TRANSCRIPT_DIR", os.path.join(_ROOT, "data", "transcripts"))
PG_SCHEMA = os.getenv("YT_PG_SCHEMA", "transcripts")

# Whisper — option A (CPU on this WSL box; no local GPU). Flip to large-v3/cuda/float16
# via env if we later move the worker to the LAN GPU host (option B).
WHISPER_MODEL = os.getenv("YT_WHISPER_MODEL", "medium")
WHISPER_DEVICE = os.getenv("YT_WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.getenv("YT_WHISPER_COMPUTE_TYPE", "int8")

# Extraction LLM — local Ollama (free, on-LAN, private; transcripts never leave the network).
OLLAMA_MODEL = os.getenv("YT_EXTRACT_MODEL", "qwen2.5:7b-instruct")


def youtube_api_key() -> str:
    k = os.getenv("YOUTUBE_API_KEY")
    if not k:
        raise RuntimeError("YOUTUBE_API_KEY not set — put it in .env")
    return k


def database_url() -> str:
    u = os.getenv("DATABASE_URL")
    if not u:
        raise RuntimeError("DATABASE_URL not set — the pipeline shares the app's Postgres")
    return u


def ollama_host() -> str:
    """Reuse the app's resolver so the pipeline self-heals across the WSL host-flip."""
    default = os.getenv("OLLAMA_HOST", "http://192.168.1.5:30068")
    try:
        from config import _resolve_ollama_host
        return _resolve_ollama_host(default)
    except Exception:  # noqa: BLE001
        return default


def watchlist() -> list[str]:
    """Tickers/names to bias Whisper's initial_prompt + focus extraction (§6, §12.4)."""
    raw = os.getenv("YT_WATCHLIST", "")
    return [w.strip() for w in raw.split(",") if w.strip()]
