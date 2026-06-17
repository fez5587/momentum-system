#!/usr/bin/env bash
# Nightly self-tuning: backfill latest bars, sweep ORB params, write
# data/learned_params.json (which run_live_paper.py reads on boot).
#
# Schedule it after the close. Examples:
#   crontab -e   ->   30 17 * * 1-5  /mnt/c/code/momentum/nightly_tune.sh
#   (WSL: cron may need `sudo service cron start`, or use Windows Task Scheduler
#    to run:  wsl bash /mnt/c/code/momentum/nightly_tune.sh )
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="/home/philip/.venvs/momentum/bin/python"
cd "$ROOT" || exit 1
mkdir -p "$ROOT/data"
echo "=== nightly_tune $(date) ===" >> "$ROOT/data/nightly_tune.log"
PYTHONPATH="$ROOT" "$VENV_PY" nightly_tune.py >> "$ROOT/data/nightly_tune.log" 2>&1
