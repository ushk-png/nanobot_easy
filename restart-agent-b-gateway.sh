#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"$SCRIPT_DIR/stop-agent-b-gateway.sh"
"$SCRIPT_DIR/start-agent-b-gateway.sh"
