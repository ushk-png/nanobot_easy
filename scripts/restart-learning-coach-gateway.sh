#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"$SCRIPT_DIR/stop-learning-coach-gateway.sh"
"$SCRIPT_DIR/start-learning-coach-gateway.sh"
