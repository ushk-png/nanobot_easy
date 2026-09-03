#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_INSTALL_DIR="${NANOBOT_EASY_HOME:-$HOME/nanobot-easy}"
CONFIG="${NANOBOT_CONFIG:-$SCRIPT_DIR/.local/config.json}"
WORKSPACE="${NANOBOT_WORKSPACE:-$SCRIPT_DIR/.local/workspace}"
NANOBOT_BIN="${NANOBOT_BIN:-$SCRIPT_DIR/.venv/bin/nanobot}"
ENV_FILE="${NANOBOT_ENV_FILE:-$SCRIPT_DIR/.local/env}"

# The actual launcher logic (config bootstrap, WebUI freshness rebuild,
# staleness-based restart, port-conflict detection, gateway start, browser
# open) lives in `nanobot up` (nanobot/cli/up.py) so it's shared with Windows
# and unit-testable. This script only does what has to happen before a
# working Python environment exists.

case "$SCRIPT_DIR" in
  "$HOME/.Trash"|"$HOME/.Trash"/*|*/.Trash/*)
    cat >&2 <<EOF
Error: this nanobot-easy checkout is inside the macOS Trash:
  $SCRIPT_DIR

Move it out of Trash or clone a fresh copy into your home directory:
  git clone https://github.com/ushk-png/nanobot_easy.git "$DEFAULT_INSTALL_DIR"
  cd "$DEFAULT_INSTALL_DIR"
  ./scripts/install-nanobot-easy.sh
EOF
    exit 1
    ;;
esac

if [[ "$SCRIPT_DIR" != "$DEFAULT_INSTALL_DIR" ]]; then
  echo "Notice: recommended nanobot-easy checkout path is $DEFAULT_INSTALL_DIR" >&2
  echo "This run will use the current checkout: $SCRIPT_DIR" >&2
fi

mkdir -p "$WORKSPACE"

if [[ ! -x "$NANOBOT_BIN" ]]; then
  echo "nanobot executable not found: $NANOBOT_BIN" >&2
  echo "Running repo-local installer first..." >&2
  "$SCRIPT_DIR/scripts/install-nanobot-easy.sh"
fi

if [[ ! -x "$NANOBOT_BIN" ]]; then
  echo "nanobot executable still not found after install: $NANOBOT_BIN" >&2
  exit 1
fi

if [[ "${NANOBOT_ENSURE_TELEGRAM:-1}" != "0" && -f "$CONFIG" ]]; then
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

start_companion_gateways() {
  # Off by default: this is a personal companion-agent feature left over from
  # before the fork was generalized, and its "warning: ... did not start"
  # noise on every run reads as a broken first-run experience to anyone who
  # doesn't use it. Opt in explicitly with NANOBOT_START_COMPANIONS=1.
  if [[ "${NANOBOT_START_COMPANIONS:-0}" != "1" ]]; then
    return 0
  fi
  if [[ "$CONFIG" != "$SCRIPT_DIR/.local/config.json" ]]; then
    return 0
  fi
  if [[ -x "$SCRIPT_DIR/scripts/start-learning-coach-gateway.sh" ]]; then
    echo "starting companion gateway: AGENT_A"
    NANOBOT_START_COMPANIONS=0 "$SCRIPT_DIR/scripts/start-learning-coach-gateway.sh" || {
      echo "warning: AGENT_A gateway did not start; see .local/logs/learning-coach-gateway.log" >&2
      return 0
    }
  fi
}

"$NANOBOT_BIN" up --config "$CONFIG" --workspace "$WORKSPACE"
status=$?
if [[ "$status" == "0" ]]; then
  start_companion_gateways
fi
exit "$status"
