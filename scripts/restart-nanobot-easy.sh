#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG="${NANOBOT_CONFIG:-$REPO_ROOT/.local/config.json}"
WORKSPACE="${NANOBOT_WORKSPACE:-$REPO_ROOT/.local/workspace}"
NANOBOT_BIN="${NANOBOT_BIN:-$REPO_ROOT/.venv/bin/nanobot}"

# Pinned deployments: when NANOBOT_RUNTIME_PROFILE is set, or a profile in
# .local/runtimes/*.json names this exact config and workspace, the instance
# runs from that profile's pinned runtime checkout (scripts/runtime.sh) so
# start, restart, per-agent wrappers and direct gateway runs select the same
# code and settings.
select_runtime_profile() {
  /usr/bin/env python3 - "$1" "$2" "$3" "${NANOBOT_RUNTIME_PROFILE:-}" <<'PY'
import json, sys
from pathlib import Path
config, workspace, profiles_dir, explicit = sys.argv[1:5]
def same(a, b):
    return Path(a).expanduser().resolve() == Path(b).expanduser().resolve()
candidates = [Path(explicit)] if explicit else sorted(Path(profiles_dir).glob("*.json"))
matches = []
for path in candidates:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        continue
    if same(data.get("config", ""), config) and same(data.get("workspace", ""), workspace):
        matches.append(str(path.resolve()))
if explicit and not matches:
    sys.exit("NANOBOT_RUNTIME_PROFILE does not match NANOBOT_CONFIG/NANOBOT_WORKSPACE")
if len(matches) > 1:
    sys.exit("several runtime profiles match this config/workspace: " + ", ".join(matches))
print(matches[0] if matches else "")
PY
}
RUNTIME_PROFILE="$(select_runtime_profile "$CONFIG" "$WORKSPACE" "$REPO_ROOT/.local/runtimes")"
if [[ -n "$RUNTIME_PROFILE" ]]; then
  exec "$REPO_ROOT/scripts/runtime.sh" "$RUNTIME_PROFILE" restart
fi

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
