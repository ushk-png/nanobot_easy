#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG="${NANOBOT_CONFIG:-$REPO_ROOT/.local/config.json}"
WORKSPACE="${NANOBOT_WORKSPACE:-$REPO_ROOT/.local/workspace}"
NANOBOT_BIN="${NANOBOT_BIN:-$REPO_ROOT/.venv/bin/nanobot}"

# The actual stop logic (tracked-process termination, ancestor-process
# self-stop guard, port-based fallback when the PID file is missing/stale)
# lives in `nanobot down` (nanobot/cli/up.py) so it's shared with Windows and
# unit-testable.

if [[ ! -x "$NANOBOT_BIN" ]]; then
  echo "nanobot executable not found: $NANOBOT_BIN" >&2
  echo "nanobot-easy gateway is not running (nothing was ever installed here)." >&2
  exit 0
fi

cd "$REPO_ROOT"
export PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"

"$NANOBOT_BIN" down --config "$CONFIG" --workspace "$WORKSPACE"
