#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="${NANOBOT_CONFIG:-$SCRIPT_DIR/.local/config.json}"
WORKSPACE="${NANOBOT_WORKSPACE:-$SCRIPT_DIR/.local/workspace}"
NANOBOT_BIN="${NANOBOT_BIN:-$SCRIPT_DIR/.venv/bin/nanobot}"
RUNTIME_DIR="${NANOBOT_RUNTIME_DIR:-$SCRIPT_DIR/.local/run}"
LOG_DIR="${NANOBOT_LOG_DIR:-$SCRIPT_DIR/.local/logs}"
PID_FILE="${NANOBOT_PID_FILE:-$RUNTIME_DIR/nanobot-skill-gateway.pid}"
LOG_FILE="${NANOBOT_LOG_FILE:-$LOG_DIR/nanobot-skill-gateway.log}"

mkdir -p "$RUNTIME_DIR" "$LOG_DIR" "$WORKSPACE"

if [[ ! -x "$NANOBOT_BIN" ]]; then
  echo "nanobot executable not found: $NANOBOT_BIN" >&2
  echo "Create the venv first, then install this project in editable mode." >&2
  exit 1
fi

if [[ ! -f "$CONFIG" ]]; then
  echo "config not found: $CONFIG" >&2
  echo "Run: PYTHONPATH=. .venv/bin/nanobot onboard --config .local/config.json --workspace .local/workspace --wizard" >&2
  exit 1
fi

if [[ -f "$PID_FILE" ]]; then
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
    echo "nanobot_skill gateway is already running: pid=$pid"
    echo "log: $LOG_FILE"
    exit 0
  fi
  rm -f "$PID_FILE"
fi

cd "$SCRIPT_DIR"
export PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}"

nohup "$NANOBOT_BIN" gateway \
  --config "$CONFIG" \
  --workspace "$WORKSPACE" \
  >> "$LOG_FILE" 2>&1 &

pid="$!"
echo "$pid" > "$PID_FILE"

sleep 2
if kill -0 "$pid" 2>/dev/null; then
  echo "nanobot_skill gateway started: pid=$pid"
  echo "config: $CONFIG"
  echo "workspace: $WORKSPACE"
  echo "log: $LOG_FILE"
else
  echo "nanobot_skill gateway failed to start. Recent log:" >&2
  tail -n 80 "$LOG_FILE" >&2 || true
  rm -f "$PID_FILE"
  exit 1
fi
