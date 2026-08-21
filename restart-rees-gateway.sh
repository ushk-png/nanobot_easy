#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"$SCRIPT_DIR/stop-rees-gateway.sh"
"$SCRIPT_DIR/start-rees-gateway.sh"
