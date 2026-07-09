#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"$SCRIPT_DIR/stop-nanobot-skill.sh"
"$SCRIPT_DIR/start-nanobot-skill.sh"
