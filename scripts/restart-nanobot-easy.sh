#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG="${NANOBOT_CONFIG:-$REPO_ROOT/.local/config.json}"
WORKSPACE="${NANOBOT_WORKSPACE:-$REPO_ROOT/.local/workspace}"
NANOBOT_BIN="${NANOBOT_BIN:-$REPO_ROOT/.venv/bin/nanobot}"

# The self-restart safety check (detached restart when called from inside the
# gateway's own process tree, so it doesn't kill its own caller mid-execution)
# lives in `nanobot restart` (nanobot/cli/up.py) now.

if [[ ! -x "$NANOBOT_BIN" ]]; then
  echo "nanobot executable not found: $NANOBOT_BIN" >&2
  exit 1
fi

cd "$REPO_ROOT"
export PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"

# Off by default, same as start-nanobot-easy.sh's start_companion_gateways():
# opt in explicitly with NANOBOT_START_COMPANIONS=1.
if [[ "${NANOBOT_START_COMPANIONS:-0}" == "1" && -x "$SCRIPT_DIR/stop-learning-coach-gateway.sh" ]]; then
  "$SCRIPT_DIR/stop-learning-coach-gateway.sh" || true
fi

"$NANOBOT_BIN" restart --config "$CONFIG" --workspace "$WORKSPACE"
status=$?

if [[ "$status" == "0" && "${NANOBOT_START_COMPANIONS:-0}" == "1" && -x "$SCRIPT_DIR/start-learning-coach-gateway.sh" ]]; then
  echo "starting companion gateway: AGENT_A"
  NANOBOT_START_COMPANIONS=0 "$SCRIPT_DIR/start-learning-coach-gateway.sh" || {
    echo "warning: AGENT_A gateway did not start; see .local/logs/learning-coach-gateway.log" >&2
  }
fi

exit "$status"
