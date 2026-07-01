"""Worker (§9): drains the pending backlog, concurrency 1, paced (INTER_VIDEO_DELAY). State
machine per video (pending -> transcribing -> extracting -> done | failed) with retries/backoff.
Each stage is independently retryable; a `done` video is never reprocessed."""

import time

from youtube_claims import config, db, extraction, transcription


def process_one(v: dict) -> str:
    """Transcribe -> extract -> insert claims for one already-claimed video. Returns final status."""
    vid = v["video_id"]
    try:
        segs, source, model, path = transcription.transcribe(vid)
    except Exception as exc:  # noqa: BLE001
        st = db.record_failure(vid, "transcribe", str(exc))
        print(f"[worker] {vid} transcribe failed -> {st}: {exc}")
        return st
    db.set_status(vid, "extracting", transcript_source=source, whisper_model=model, transcript_path=path)
    try:
        claims = extraction.extract_claims(segs)
    except Exception as exc:  # noqa: BLE001
        st = db.record_failure(vid, "extract", str(exc))
        print(f"[worker] {vid} extract failed -> {st}: {exc}")
        return st
    n = db.insert_claims(vid, claims)
    db.mark_done(vid, source, model, path)
    print(f"[worker] {vid} done: {len(segs)} segments ({source}), {n} claims")
    return "done"


def drain(max_videos: int | None = None) -> int:
    """Process pending videos one at a time with a politeness gap. Returns #processed."""
    done = 0
    while True:
        v = db.claim_next_pending()
        if v is None:
            break
        process_one(v)
        done += 1
        if max_videos and done >= max_videos:
            break
        time.sleep(config.INTER_VIDEO_DELAY_SECONDS)   # §13 — keep yt-dlp under the rate cap
    return done
