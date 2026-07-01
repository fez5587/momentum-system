"""Orchestration (§2): one long-lived process. APScheduler fires the 30-min poll; a worker
loop drains pending videos alongside it. On restart nothing is lost — pending rows resume from
Postgres. The same CLI exposes each piece for setup + manual runs.

  python -m youtube_claims.run apply-schema
  python -m youtube_claims.run add-playlist <playlist_id> --label "..." --content-type analysis
  python -m youtube_claims.run poll-once            # detection pass only
  python -m youtube_claims.run drain --max 1        # process N pending videos then stop
  python -m youtube_claims.run run                  # the full service (poll + worker)
"""

import argparse
import threading
import time

from youtube_claims import config, db, detection, worker


def _worker_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            if worker.drain() == 0:
                stop.wait(30)           # nothing pending — idle until the next poll fills it
        except Exception as exc:  # noqa: BLE001 — the loop must survive one bad video
            print(f"[worker-loop] {exc}")
            stop.wait(30)


def run_service() -> None:
    from apscheduler.schedulers.background import BackgroundScheduler
    db.apply_schema()

    def _poll():
        try:
            new, used = detection.poll_once()
            print(f"[poll] {new} new videos, {used} api units today")
        except Exception as exc:  # noqa: BLE001
            print(f"[poll] failed: {exc}")

    sched = BackgroundScheduler()
    sched.add_job(_poll, "interval", minutes=config.POLL_INTERVAL_MINUTES)
    sched.start()
    _poll()                              # once at boot
    stop = threading.Event()
    threading.Thread(target=_worker_loop, args=(stop,), daemon=True).start()
    print(f"[service] running: poll every {config.POLL_INTERVAL_MINUTES}m, worker concurrency 1")
    try:
        while True:
            time.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        stop.set()
        sched.shutdown()


def main() -> None:
    ap = argparse.ArgumentParser(description="YouTube -> transcript -> claims pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("apply-schema")
    a = sub.add_parser("add-playlist")
    a.add_argument("playlist_id")
    a.add_argument("--label", default="")
    a.add_argument("--content-type", default="unknown")
    sub.add_parser("poll-once")
    d = sub.add_parser("drain")
    d.add_argument("--max", type=int)
    sub.add_parser("run")
    args = ap.parse_args()

    if args.cmd == "apply-schema":
        db.apply_schema()
        print(f"schema '{config.PG_SCHEMA}' applied")
    elif args.cmd == "add-playlist":
        db.apply_schema()
        db.upsert_playlist(args.playlist_id, args.label, args.content_type)
        print(f"playlist {args.playlist_id} added ({args.label!r}, {args.content_type})")
    elif args.cmd == "poll-once":
        new, used = detection.poll_once()
        print(f"{new} new videos inserted, {used} api units used today")
    elif args.cmd == "drain":
        print("processed", worker.drain(max_videos=args.max))
    elif args.cmd == "run":
        run_service()


if __name__ == "__main__":
    main()
