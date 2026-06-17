#!/usr/bin/env bash
# Capture the day's trade journal at the close into a durable daily record.
#   crontab: 2 16 * * 1-5  /mnt/c/code/momentum/journal.sh
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="/home/philip/.venvs/momentum/bin/python"
cd "$ROOT" || exit 1
mkdir -p "$ROOT/data"
{ echo; echo "=================================================="; date; } >> "$ROOT/data/trade_journal.log"
PYTHONPATH="$ROOT" TERM=dumb "$VENV_PY" momentum_cli.py journal >> "$ROOT/data/trade_journal.log" 2>&1
