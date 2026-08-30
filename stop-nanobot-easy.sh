#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_DIR="${NANOBOT_RUNTIME_DIR:-$SCRIPT_DIR/.local/run}"
LOG_DIR="${NANOBOT_LOG_DIR:-$SCRIPT_DIR/.local/logs}"
PID_FILE="${NANOBOT_PID_FILE:-$RUNTIME_DIR/nanobot-easy-gateway.pid}"
LOG_FILE="${NANOBOT_LOG_FILE:-$LOG_DIR/nanobot-easy-gateway.log}"
CONFIG="${NANOBOT_CONFIG:-$SCRIPT_DIR/.local/config.json}"

# Same fallback port-detection as start-nanobot-easy.sh, for when the PID
# file is missing or stale but a gateway is still actually bound to a port
# (e.g. it was force-killed and its detached child survived).
port_in_use() {
  local host="$1" port="$2"
  "$SCRIPT_DIR/.venv/bin/python" - "$host" "$port" <<'PY' >/dev/null 2>&1
import socket
import sys

host, port = sys.argv[1], int(sys.argv[2])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(0.5)
try:
    s.connect((host, port))
except OSError:
    sys.exit(1)
else:
    sys.exit(0)
finally:
    s.close()
PY
}

pid_on_port() {
  local port="$1"
  local lsof_bin=""
  if command -v lsof >/dev/null 2>&1; then
    lsof_bin="$(command -v lsof)"
  elif [[ -x /usr/sbin/lsof ]]; then
    lsof_bin="/usr/sbin/lsof"
  fi
  if [[ -n "$lsof_bin" ]]; then
    "$lsof_bin" -nP -t -i "tcp:${port}" -sTCP:LISTEN 2>/dev/null | head -n1
    return 0
  fi
  if command -v fuser >/dev/null 2>&1; then
    fuser "${port}/tcp" 2>/dev/null | tr -d '[:space:]'
    return 0
  fi
  "$SCRIPT_DIR/.venv/bin/python" - "$port" <<'PY' 2>/dev/null
import sys

port = int(sys.argv[1])
target = f"{port:04X}"
try:
    with open("/proc/net/tcp") as f:
        next(f)
        for line in f:
            local = line.split()[1]
            if local.split(":")[1] == target:
                inode = line.split()[9]
                break
        else:
            sys.exit(0)
except OSError:
    sys.exit(0)

import os

for pid_dir in os.listdir("/proc"):
    if not pid_dir.isdigit():
        continue
    fd_dir = f"/proc/{pid_dir}/fd"
    try:
        for fd in os.listdir(fd_dir):
            try:
                link = os.readlink(f"{fd_dir}/{fd}")
            except OSError:
                continue
            if link == f"socket:[{inode}]":
                print(pid_dir)
                sys.exit(0)
    except OSError:
        continue
PY
}

ports_from_config() {
  [[ -f "$CONFIG" ]] || return 0
  "$SCRIPT_DIR/.venv/bin/python" - "$CONFIG" <<'PY' 2>/dev/null
import json
import sys
from pathlib import Path

try:
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, ValueError):
    sys.exit(0)
gateway = data.get("gateway") or {}
websocket = (data.get("channels") or {}).get("websocket") or {}
print(gateway.get("host") or "127.0.0.1", int(gateway.get("port") or 18790))
print(websocket.get("host") or "127.0.0.1", int(websocket.get("port") or 8765))
PY
}

stop_by_port_fallback() {
  local found=0
  while read -r host port; do
    [[ -z "${host:-}" ]] && continue
    if port_in_use "$host" "$port"; then
      found=1
      local blocking_pid
      blocking_pid="$(pid_on_port "$port")"
      if [[ -n "$blocking_pid" ]]; then
        echo "found an untracked process on $host:$port: pid=$blocking_pid ($(ps -o comm= -p "$blocking_pid" 2>/dev/null || echo unknown))"
        echo "stopping it: pid=$blocking_pid"
        kill "$blocking_pid" 2>/dev/null || true
        sleep 1
        kill -0 "$blocking_pid" 2>/dev/null && kill -KILL "$blocking_pid" 2>/dev/null || true
      else
        echo "something is listening on $host:$port but its pid could not be determined." >&2
        echo "install lsof or fuser, or find it manually with: ss -ltnp | grep -E \":$port\\b\"" >&2
      fi
    fi
  done < <(ports_from_config)
  return $((1 - found))
}

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
  if stop_by_port_fallback; then
    exit 0
  fi
  echo "nothing found on the configured ports either."
  exit 0
fi

pid="$(cat "$PID_FILE" 2>/dev/null || true)"
if [[ -z "${pid:-}" ]]; then
  rm -f "$PID_FILE"
  echo "nanobot-easy gateway is not running: empty pid file removed"
  if stop_by_port_fallback; then
    exit 0
  fi
  echo "nothing found on the configured ports either."
  exit 0
fi

if ! kill -0 "$pid" 2>/dev/null; then
  rm -f "$PID_FILE"
  echo "nanobot-easy gateway is not running: stale pid file removed"
  if stop_by_port_fallback; then
    exit 0
  fi
  echo "nothing found on the configured ports either."
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
