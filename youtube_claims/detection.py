"""Detection layer (§5): poll each enabled playlist, dedupe by video_id, insert `pending`
rows. Uses the YouTube Data API `playlistItems.list` over urllib (no extra SDK dep)."""

import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from youtube_claims import config, db


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.load(r)


def _iso(s: str | None):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def fetch_playlist_items(playlist_id: str, key: str) -> tuple[list[dict], int]:
    """All items of a playlist, paginated. Returns (items, api_units_used ~= #pages)."""
    items, token, units = [], None, 0
    while True:
        q = {"part": "snippet,contentDetails", "maxResults": config.PLAYLIST_PAGE_SIZE,
             "playlistId": playlist_id, "key": key}
        if token:
            q["pageToken"] = token
        data = _get("https://www.googleapis.com/youtube/v3/playlistItems?" + urllib.parse.urlencode(q))
        units += 1
        items += data.get("items", [])
        token = data.get("nextPageToken")
        if not token:
            break
    return items, units


def poll_once() -> tuple[int, int]:
    """One detection pass over all enabled playlists. Returns (#new videos, quota used today)."""
    key = config.youtube_api_key()
    seen = db.seen_video_ids()
    new_count, units = 0, 0
    for pl in db.enabled_playlists():
        try:
            items, u = fetch_playlist_items(pl["playlist_id"], key)
        except Exception as exc:  # noqa: BLE001 — one bad playlist can't stop the poll
            print(f"[detect] playlist {pl['playlist_id']} poll failed: {exc}")
            continue
        units += u
        now = datetime.now(timezone.utc)
        for it in items:
            sn = it["snippet"]
            vid = it["contentDetails"]["videoId"]
            if vid in seen:
                continue
            v = dict(
                video_id=vid, playlist_id=pl["playlist_id"],
                channel_id=sn.get("videoOwnerChannelId") or sn.get("channelId"),
                channel_name=sn.get("videoOwnerChannelTitle") or sn.get("channelTitle"),
                title=sn.get("title"),
                content_type=pl.get("content_type", "unknown"),
                published_at=_iso(it["contentDetails"].get("videoPublishedAt") or sn.get("publishedAt")),
                detected_at=now)
            if db.insert_pending_video(v):
                new_count += 1
                seen.add(vid)
    used = db.add_quota(units)
    if used > config.YOUTUBE_API_QUOTA_DAILY * 0.8:
        print(f"[detect] WARNING: {used}/{config.YOUTUBE_API_QUOTA_DAILY} daily API units used")
    return new_count, used
