#!/bin/sh
# Run the mock broker + web UI in the background (survives logout).
#   scripts/mock-bg.sh start|stop|status|log
set -eu
cd "$(dirname "$0")/.."
PIDF=/tmp/rigbroker-mock.pid
LOG=/tmp/rigbroker-mock.log
PORT=${PORT:-8080}

running() { [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; }

case "${1:-status}" in
  start)
    if running; then echo "already running (pid $(cat "$PIDF"))"; exit 0; fi
    setsid .venv/bin/python -m rigbroker --mock --allow-tx --port "$PORT" >"$LOG" 2>&1 </dev/null &
    echo $! >"$PIDF"
    sleep 3
    if running; then echo "started pid $(cat "$PIDF") - http://$(hostname -I | cut -d' ' -f1):$PORT/"
    else echo "failed to start:"; cat "$LOG"; exit 1; fi ;;
  stop)
    if running; then kill "$(cat "$PIDF")"; sleep 1; echo stopped; else echo "not running"; fi
    rm -f "$PIDF" ;;
  status) if running; then echo "running pid $(cat "$PIDF")"; else echo "not running"; fi ;;
  log) tail -n "${2:-40}" "$LOG" ;;
  *) echo "usage: $0 start|stop|status|log [lines]"; exit 2 ;;
esac
