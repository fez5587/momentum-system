"""Transcription (§6): yt-dlp bestaudio -> faster-whisper (initial_prompt seeded with the
watchlist) with a youtube-transcript-api captions FALLBACK. Whisper is PRIMARY because
auto-captions mangle the load-bearing tokens (tickers/numbers/prices); captions are a
last resort so a blocked/failed download still yields something. Writes transcript JSON."""

import json
import os
import tempfile

from youtube_claims import config

_model = None


def _whisper():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        _model = WhisperModel(config.WHISPER_MODEL, device=config.WHISPER_DEVICE,
                              compute_type=config.WHISPER_COMPUTE_TYPE)
    return _model


def _download_audio(video_id: str, out_dir: str) -> str | None:
    import yt_dlp
    tmpl = os.path.join(out_dir, f"{video_id}.%(ext)s")
    opts = {"format": config.YT_DLP_FORMAT, "outtmpl": tmpl, "quiet": True, "no_warnings": True,
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "wav"}]}
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
    wav = os.path.join(out_dir, f"{video_id}.wav")
    return wav if os.path.exists(wav) else None


def _transcribe_whisper(wav: str, watchlist: list[str]) -> list[dict]:
    prompt = ("Tickers and companies: " + ", ".join(watchlist)) if watchlist else None
    segments, _info = _whisper().transcribe(wav, initial_prompt=prompt, vad_filter=True)
    return [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text} for s in segments]


def _captions_fallback(video_id: str) -> list[dict]:
    from youtube_transcript_api import YouTubeTranscriptApi
    try:                                            # newer + older API shapes
        rows = YouTubeTranscriptApi().fetch(video_id).to_raw_data()
    except Exception:  # noqa: BLE001
        rows = YouTubeTranscriptApi.get_transcript(video_id)
    return [{"start": round(x["start"], 2), "end": round(x["start"] + x.get("duration", 0), 2),
             "text": x["text"]} for x in rows]


def transcribe(video_id: str, watchlist: list[str] | None = None) -> tuple[list[dict], str, str | None, str]:
    """Returns (segments, source, whisper_model_or_None, transcript_path). Whisper primary,
    captions fallback. Raises only if BOTH paths fail (worker handles the retry)."""
    watchlist = watchlist if watchlist is not None else config.watchlist()
    os.makedirs(config.TRANSCRIPT_DIR, exist_ok=True)
    segs, source, model = None, None, None
    try:
        with tempfile.TemporaryDirectory() as td:
            wav = _download_audio(video_id, td)
            if wav:
                segs = _transcribe_whisper(wav, watchlist)
                source, model = "whisper", config.WHISPER_MODEL
    except Exception as exc:  # noqa: BLE001 — fall through to captions
        print(f"[transcribe] whisper path failed for {video_id}: {exc}")
    if not segs:
        segs = _captions_fallback(video_id)         # may raise -> worker retry/backoff
        source = "captions"
    path = os.path.join(config.TRANSCRIPT_DIR, f"{video_id}.json")
    with open(path, "w") as f:
        json.dump({"video_id": video_id, "source": source, "whisper_model": model, "segments": segs}, f)
    return segs, source, model, path
