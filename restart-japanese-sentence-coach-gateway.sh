#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"$SCRIPT_DIR/stop-japanese-sentence-coach-gateway.sh"
"$SCRIPT_DIR/start-japanese-sentence-coach-gateway.sh"
