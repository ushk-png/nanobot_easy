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
ENV_FILE="${NANOBOT_ENV_FILE:-$SCRIPT_DIR/.local/env}"

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

prepend_path_once() {
  local dir="$1"
  [[ -d "$dir" ]] || return 0
  case ":$PATH:" in
    *":$dir:"*) ;;
    *) PATH="$dir:$PATH" ;;
  esac
}

prepend_path_once "/opt/homebrew/bin"
prepend_path_once "/opt/homebrew/sbin"
prepend_path_once "/usr/local/bin"
prepend_path_once "/usr/local/sbin"
export PATH

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

pid="$(
  "$SCRIPT_DIR/.venv/bin/python" - "$NANOBOT_BIN" "$CONFIG" "$WORKSPACE" "$LOG_FILE" "$SCRIPT_DIR" <<'PY'
import os
import subprocess
import sys
from pathlib import Path

nanobot_bin, config, workspace, log_file, cwd = sys.argv[1:]
Path(log_file).parent.mkdir(parents=True, exist_ok=True)
env = os.environ.copy()
log = open(log_file, "ab", buffering=0)
proc = subprocess.Popen(
    [nanobot_bin, "gateway", "--config", config, "--workspace", workspace],
    cwd=cwd,
    stdin=subprocess.DEVNULL,
    stdout=log,
    stderr=subprocess.STDOUT,
    env=env,
    start_new_session=True,
    close_fds=True,
)
print(proc.pid)
PY
)"
echo "$pid" > "$PID_FILE"

sleep 2
if kill -0 "$pid" 2>/dev/null; then
  for _ in {1..20}; do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "nanobot_skill gateway exited during startup. Recent log:" >&2
      tail -n 80 "$LOG_FILE" >&2 || true
      rm -f "$PID_FILE"
      exit 1
    fi
    if curl -fsS --max-time 1 "http://127.0.0.1:18790/health" >/dev/null 2>&1; then
      echo "nanobot_skill gateway started: pid=$pid"
      echo "config: $CONFIG"
      echo "workspace: $WORKSPACE"
      echo "log: $LOG_FILE"
      exit 0
    fi
    sleep 0.5
  done
  echo "nanobot_skill gateway started but health check did not become ready yet: pid=$pid"
  echo "config: $CONFIG"
  echo "workspace: $WORKSPACE"
  echo "log: $LOG_FILE"
else
  echo "nanobot_skill gateway failed to start. Recent log:" >&2
  tail -n 80 "$LOG_FILE" >&2 || true
  rm -f "$PID_FILE"
  exit 1
fi
