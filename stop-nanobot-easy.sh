#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_DIR="${NANOBOT_RUNTIME_DIR:-$SCRIPT_DIR/.local/run}"
LOG_DIR="${NANOBOT_LOG_DIR:-$SCRIPT_DIR/.local/logs}"
PID_FILE="${NANOBOT_PID_FILE:-$RUNTIME_DIR/nanobot-easy-gateway.pid}"
LOG_FILE="${NANOBOT_LOG_FILE:-$LOG_DIR/nanobot-easy-gateway.log}"

is_ancestor_pid() {
  local needle="$1"
  local pid="$$"
  while [[ -n "${pid:-}" && "$pid" != "0" && "$pid" != "1" ]]; do
    if [[ "$pid" == "$needle" ]]; then
      return 0
    fi
    pid="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d '[:space:]' || true)"
  done
  return 1
}

if [[ ! -f "$PID_FILE" ]]; then
  echo "nanobot-easy gateway is not running: pid file not found"
  exit 0
fi

pid="$(cat "$PID_FILE" 2>/dev/null || true)"
if [[ -z "${pid:-}" ]]; then
  rm -f "$PID_FILE"
  echo "nanobot-easy gateway is not running: empty pid file removed"
  exit 0
fi

if ! kill -0 "$pid" 2>/dev/null; then
  rm -f "$PID_FILE"
  echo "nanobot-easy gateway is not running: stale pid file removed"
  exit 0
fi

if [[ "${NANOBOT_ALLOW_SELF_STOP:-0}" != "1" ]] && is_ancestor_pid "$pid"; then
  echo "refusing to stop nanobot-easy gateway from inside its own process tree" >&2
  echo "use restart-nanobot-easy.sh, which schedules a detached restart safely" >&2
  exit 2
fi

echo "stopping nanobot-easy gateway: pid=$pid"
kill "$pid" 2>/dev/null || true

for _ in {1..20}; do
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "nanobot-easy gateway stopped"
    echo "log: $LOG_FILE"
    exit 0
  fi
  sleep 0.5
done

echo "gateway did not stop after TERM; sending KILL: pid=$pid" >&2
kill -KILL "$pid" 2>/dev/null || true
rm -f "$PID_FILE"
echo "nanobot-easy gateway stopped"
echo "log: $LOG_FILE"
