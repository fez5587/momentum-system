#!/usr/bin/env bash
# End-of-day whole-market replay: what today's logic would have done + grow the
# stored dataset for the tuner/verification. Schedule right after the close,
# before nightly_tune (which then tunes on the freshly-grown data).
#   crontab -e  ->  5 16 * * 1-5  /mnt/c/code/momentum/eod_replay.sh
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="/home/philip/.venvs/momentum/bin/python"
cd "$ROOT" || exit 1
mkdir -p "$ROOT/data"
echo "=== eod_replay $(date) ===" >> "$ROOT/data/eod_replay.log"
PYTHONPATH="$ROOT" "$VENV_PY" eod_replay.py >> "$ROOT/data/eod_replay.log" 2>&1
