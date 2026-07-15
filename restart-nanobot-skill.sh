#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"$SCRIPT_DIR/stop-nanobot-skill.sh"
if [[ -x "$SCRIPT_DIR/stop-japanese-sentence-coach-gateway.sh" ]]; then
  "$SCRIPT_DIR/stop-japanese-sentence-coach-gateway.sh"
fi
"$SCRIPT_DIR/start-nanobot-skill.sh"
