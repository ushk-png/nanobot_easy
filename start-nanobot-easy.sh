#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_INSTALL_DIR="${NANOBOT_EASY_HOME:-$HOME/nanobot-easy}"
CONFIG="${NANOBOT_CONFIG:-$SCRIPT_DIR/.local/config.json}"
WORKSPACE="${NANOBOT_WORKSPACE:-$SCRIPT_DIR/.local/workspace}"
NANOBOT_BIN="${NANOBOT_BIN:-$SCRIPT_DIR/.venv/bin/nanobot}"
RUNTIME_DIR="${NANOBOT_RUNTIME_DIR:-$SCRIPT_DIR/.local/run}"
LOG_DIR="${NANOBOT_LOG_DIR:-$SCRIPT_DIR/.local/logs}"
PID_FILE="${NANOBOT_PID_FILE:-$RUNTIME_DIR/nanobot-easy-gateway.pid}"
LOG_FILE="${NANOBOT_LOG_FILE:-$LOG_DIR/nanobot-easy-gateway.log}"
ENV_FILE="${NANOBOT_ENV_FILE:-$SCRIPT_DIR/.local/env}"
WEBUI_DIST_INDEX="$SCRIPT_DIR/nanobot/web/dist/index.html"
WEBUI_SRC_DIR="$SCRIPT_DIR/webui"

case "$SCRIPT_DIR" in
  "$HOME/.Trash"|"$HOME/.Trash"/*|*/.Trash/*)
    cat >&2 <<EOF
Error: this nanobot-easy checkout is inside the macOS Trash:
  $SCRIPT_DIR

Move it out of Trash or clone a fresh copy into your home directory:
  git clone https://github.com/ushk-png/nanobot_easy.git "$DEFAULT_INSTALL_DIR"
  cd "$DEFAULT_INSTALL_DIR"
  ./install-nanobot-easy.sh
EOF
    exit 1
    ;;
esac

if [[ "$SCRIPT_DIR" != "$DEFAULT_INSTALL_DIR" ]]; then
  echo "Notice: recommended nanobot-easy checkout path is $DEFAULT_INSTALL_DIR" >&2
  echo "This run will use the current checkout: $SCRIPT_DIR" >&2
fi

# Same staleness check as install-nanobot-easy.sh's ensure_webui_dist: a
# `git pull` that only updates webui/src would otherwise keep serving a WebUI
# bundle built from the old source, since index.html already exists.
webui_dist_is_fresh() {
  local index_html="$1"
  if [[ -f "$WEBUI_SRC_DIR/package.json" && "$WEBUI_SRC_DIR/package.json" -nt "$index_html" ]]; then
    return 1
  fi
  if [[ -d "$WEBUI_SRC_DIR/src" ]] && find "$WEBUI_SRC_DIR/src" -type f -newer "$index_html" -print -quit 2>/dev/null | grep -q .; then
    return 1
  fi
  return 0
}

mkdir -p "$RUNTIME_DIR" "$LOG_DIR" "$WORKSPACE"

if [[ ! -x "$NANOBOT_BIN" ]]; then
  echo "nanobot executable not found: $NANOBOT_BIN" >&2
  echo "Running repo-local installer first..." >&2
  "$SCRIPT_DIR/install-nanobot-easy.sh"
fi

if [[ ! -x "$NANOBOT_BIN" ]]; then
  echo "nanobot executable still not found after install: $NANOBOT_BIN" >&2
  exit 1
fi

if [[ ! -f "$WEBUI_DIST_INDEX" ]] || ! webui_dist_is_fresh "$WEBUI_DIST_INDEX"; then
  if [[ -f "$WEBUI_DIST_INDEX" ]]; then
    echo "WebUI source has changed since the last build: $WEBUI_DIST_INDEX is stale" >&2
  else
    echo "WebUI bundle not found: $WEBUI_DIST_INDEX" >&2
  fi
  echo "Running installer to (re)build the WebUI bundle..." >&2
  NANOBOT_SKIP_WIZARD=1 NANOBOT_FORCE_WEBUI_BUILD=1 "$SCRIPT_DIR/install-nanobot-easy.sh"
fi

if [[ ! -f "$WEBUI_DIST_INDEX" ]]; then
  echo "WebUI bundle still not found after install: $WEBUI_DIST_INDEX" >&2
  exit 1
fi

if [[ ! -f "$CONFIG" ]]; then
  echo "config not found: $CONFIG" >&2
  echo "Creating a default config — finish setup in the browser (WebUI) once nanobot-easy starts." >&2
  PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}" "$NANOBOT_BIN" onboard \
    --config "$CONFIG" \
    --workspace "$WORKSPACE"
fi

if [[ ! -f "$CONFIG" ]]; then
  echo "config still not found after setup: $CONFIG" >&2
  exit 1
fi

if [[ "${NANOBOT_ENSURE_TELEGRAM:-1}" != "0" ]]; then
  "$SCRIPT_DIR/.venv/bin/python" - "$CONFIG" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))
channels = data.setdefault("channels", {})
telegram = channels.setdefault("telegram", {})
token = str(telegram.get("token") or "").strip()
if not token:
    print("telegram channel guard: token is empty; Telegram will not connect", file=sys.stderr)
elif telegram.get("enabled") is not True:
    telegram["enabled"] = True
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("telegram channel guard: enabled channels.telegram because a bot token is configured")
else:
    print("telegram channel guard: enabled")
PY
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

PAGEAGENT_ENV_FILE="${PAGEAGENT_ENV_FILE:-$WORKSPACE/.secrets/relay/page-agent.env}"
if [[ -f "$PAGEAGENT_ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$PAGEAGENT_ENV_FILE"
  set +a
fi

open_browser_if_available() {
  if [[ "${NANOBOT_OPEN_BROWSER:-1}" == "0" ]]; then
    return 0
  fi
  local url="$1"
  if command -v open >/dev/null 2>&1; then
    open "$url" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url" >/dev/null 2>&1 || true
  fi
}

# Checks whether something is already listening on host:port, independent of
# our own PID file (which can go stale if a previous run crashed, was killed
# with SIGKILL, or was left in a detached session). Uses only the Python
# standard library so it works without lsof/fuser/ss installed.
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
    sys.exit(1)  # nothing listening -> port is free
else:
    sys.exit(0)  # connected -> something is listening
finally:
    s.close()
PY
}

# Best-effort: find the PID of whatever is listening on a port, without
# requiring lsof/fuser to be installed. Falls back to nothing (empty string)
# if we can't determine it; callers must handle that.
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

start_companion_gateways() {
  if [[ "${NANOBOT_START_COMPANIONS:-1}" == "0" ]]; then
    return 0
  fi
  if [[ "$CONFIG" != "$SCRIPT_DIR/.local/config.json" ]]; then
    return 0
  fi
  if [[ -x "$SCRIPT_DIR/start-japanese-sentence-coach-gateway.sh" ]]; then
    echo "starting companion gateway: 엘르"
    NANOBOT_START_COMPANIONS=0 "$SCRIPT_DIR/start-japanese-sentence-coach-gateway.sh" || {
      echo "warning: 엘르 gateway did not start; see .local/logs/japanese-sentence-coach-gateway.log" >&2
      return 0
    }
  fi
}

read -r GATEWAY_HOST GATEWAY_PORT WEBUI_HOST WEBUI_PORT < <("$SCRIPT_DIR/.venv/bin/python" - "$CONFIG" <<'PY'
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
gateway = data.get("gateway") or {}
websocket = (data.get("channels") or {}).get("websocket") or {}
print(
    gateway.get("host") or "127.0.0.1",
    int(gateway.get("port") or 18790),
    websocket.get("host") or "127.0.0.1",
    int(websocket.get("port") or 8765),
)
PY
)

# Keep the Vite WebUI dev server pointed at the gateway's WebSocket HTTP API.
# The health endpoint uses gateway.port, but /webui/bootstrap is served by
# channels.websocket.port, so a stale default here causes "bootstrap failed".
"$SCRIPT_DIR/.venv/bin/python" - "$CONFIG" "$SCRIPT_DIR/webui/.env.local" <<'PY'
import json
import sys
from pathlib import Path

config_path = Path(sys.argv[1])
env_path = Path(sys.argv[2])
data = json.loads(config_path.read_text(encoding="utf-8"))
websocket = (data.get("channels") or {}).get("websocket") or {}
host = websocket.get("host") or "127.0.0.1"
port = int(websocket.get("port") or 8765)
env_path.parent.mkdir(parents=True, exist_ok=True)
env_path.write_text(f"NANOBOT_API_URL=http://{host}:{port}\n", encoding="utf-8")
print(f"webui env: NANOBOT_API_URL=http://{host}:{port}")
PY

if [[ -f "$PID_FILE" ]]; then
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
    echo "nanobot-easy gateway is already running: pid=$pid"
    echo "log: $LOG_FILE"
    start_companion_gateways
    exit 0
  fi
  rm -f "$PID_FILE"
fi

# The PID file above says nothing is running, but something may still be
# bound to our ports (e.g. a previous run that was force-killed or left a
# detached child behind). Starting anyway would just crash with a confusing
# "address already in use" traceback, so check first and fail clearly.
if port_in_use "$GATEWAY_HOST" "$GATEWAY_PORT" || port_in_use "$WEBUI_HOST" "$WEBUI_PORT"; then
  echo "nanobot-easy gateway ports look busy, but no tracked process is running:" >&2
  echo "  $GATEWAY_HOST:$GATEWAY_PORT (health) / $WEBUI_HOST:$WEBUI_PORT (websocket)" >&2
  blocking_pid="$(pid_on_port "$GATEWAY_PORT")"
  [[ -z "$blocking_pid" ]] && blocking_pid="$(pid_on_port "$WEBUI_PORT")"
  if [[ -n "$blocking_pid" ]]; then
    echo "  likely culprit: pid=$blocking_pid ($(ps -o comm= -p "$blocking_pid" 2>/dev/null || echo unknown))" >&2
    echo "  stop it with: kill $blocking_pid" >&2
  else
    echo "  could not identify the process automatically (install lsof or fuser for that)." >&2
    echo "  find it with: ss -ltnp | grep -E ':($GATEWAY_PORT|$WEBUI_PORT)\\b'" >&2
  fi
  echo "  or set NANOBOT_CONFIG to use different ports in .local/config.json (gateway.port / channels.websocket.port)." >&2
  exit 1
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
      echo "nanobot-easy gateway exited during startup. Recent log:" >&2
      tail -n 80 "$LOG_FILE" >&2 || true
      rm -f "$PID_FILE"
      exit 1
    fi
    if curl -fsS --max-time 1 "http://$GATEWAY_HOST:$GATEWAY_PORT/health" >/dev/null 2>&1; then
      echo "nanobot-easy gateway started: pid=$pid"
      echo "config: $CONFIG"
      echo "workspace: $WORKSPACE"
      echo "health: http://$GATEWAY_HOST:$GATEWAY_PORT/health"
      echo "webui: http://$WEBUI_HOST:$WEBUI_PORT/"
      echo "log: $LOG_FILE"
      open_browser_if_available "http://$WEBUI_HOST:$WEBUI_PORT/"
      start_companion_gateways
      exit 0
    fi
    sleep 0.5
  done
  echo "nanobot-easy gateway started but health check did not become ready yet: pid=$pid"
  echo "config: $CONFIG"
  echo "workspace: $WORKSPACE"
  echo "webui: http://$WEBUI_HOST:$WEBUI_PORT/"
  echo "log: $LOG_FILE"
else
  echo "nanobot-easy gateway failed to start. Recent log:" >&2
  tail -n 80 "$LOG_FILE" >&2 || true
  rm -f "$PID_FILE"
  exit 1
fi
