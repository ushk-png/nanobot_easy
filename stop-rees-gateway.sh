#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export NANOBOT_PID_FILE="${NANOBOT_PID_FILE:-$SCRIPT_DIR/.local/run/rees-gateway.pid}"
export NANOBOT_LOG_FILE="${NANOBOT_LOG_FILE:-$SCRIPT_DIR/.local/logs/rees-gateway.log}"

exec "$SCRIPT_DIR/stop-nanobot-easy.sh"
