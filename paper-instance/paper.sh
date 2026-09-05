#!/usr/bin/env bash
# Parallel paper-trading instance of rh-copybot ($100K simulated bankroll).
#   ./paper.sh          run
#   ./paper.sh status   positions / PnL / bankroll
cd "$(dirname "$0")"
PY=../rh-copybot/.venv/bin/python; [ -x "$PY" ] || PY=python3
BOT_HOME="$(pwd)" exec "$PY" ../rh-copybot/bot.py "$@"
