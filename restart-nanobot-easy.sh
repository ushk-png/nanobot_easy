#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_DIR="${NANOBOT_RUNTIME_DIR:-$SCRIPT_DIR/.local/run}"
LOG_DIR="${NANOBOT_LOG_DIR:-$SCRIPT_DIR/.local/logs}"
PID_FILE="${NANOBOT_PID_FILE:-$RUNTIME_DIR/nanobot-easy-gateway.pid}"
RESTART_LOG="${NANOBOT_RESTART_LOG:-$LOG_DIR/nanobot-easy-restart.log}"

mkdir -p "$RUNTIME_DIR" "$LOG_DIR"

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

if [[ "${NANOBOT_DETACHED_RESTART:-0}" != "1" && -f "$PID_FILE" ]]; then
  current_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "${current_pid:-}" ]] && kill -0 "$current_pid" 2>/dev/null && is_ancestor_pid "$current_pid"; then
    echo "restart requested from inside nanobot gateway; scheduling detached restart"
    (
      sleep "${NANOBOT_DETACHED_RESTART_DELAY:-5}"
      NANOBOT_DETACHED_RESTART=1 "$SCRIPT_DIR/restart-nanobot-easy.sh"
    ) >>"$RESTART_LOG" 2>&1 &
    disown 2>/dev/null || true
    echo "detached restart scheduled; log: $RESTART_LOG"
    exit 0
  fi
fi

"$SCRIPT_DIR/stop-nanobot-easy.sh"
if [[ -x "$SCRIPT_DIR/stop-japanese-sentence-coach-gateway.sh" ]]; then
  "$SCRIPT_DIR/stop-japanese-sentence-coach-gateway.sh"
fi
"$SCRIPT_DIR/start-nanobot-easy.sh"
