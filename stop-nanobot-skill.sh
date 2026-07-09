#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_DIR="${NANOBOT_RUNTIME_DIR:-$SCRIPT_DIR/.local/run}"
LOG_DIR="${NANOBOT_LOG_DIR:-$SCRIPT_DIR/.local/logs}"
PID_FILE="${NANOBOT_PID_FILE:-$RUNTIME_DIR/nanobot-skill-gateway.pid}"
LOG_FILE="${NANOBOT_LOG_FILE:-$LOG_DIR/nanobot-skill-gateway.log}"

if [[ ! -f "$PID_FILE" ]]; then
  echo "nanobot_skill gateway is not running: pid file not found"
  exit 0
fi

pid="$(cat "$PID_FILE" 2>/dev/null || true)"
if [[ -z "${pid:-}" ]]; then
  rm -f "$PID_FILE"
  echo "nanobot_skill gateway is not running: empty pid file removed"
  exit 0
fi

if ! kill -0 "$pid" 2>/dev/null; then
  rm -f "$PID_FILE"
  echo "nanobot_skill gateway is not running: stale pid file removed"
  exit 0
fi

echo "stopping nanobot_skill gateway: pid=$pid"
kill "$pid" 2>/dev/null || true

for _ in {1..20}; do
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "nanobot_skill gateway stopped"
    echo "log: $LOG_FILE"
    exit 0
  fi
  sleep 0.5
done

echo "gateway did not stop after TERM; sending KILL: pid=$pid" >&2
kill -KILL "$pid" 2>/dev/null || true
rm -f "$PID_FILE"
echo "nanobot_skill gateway stopped"
echo "log: $LOG_FILE"
