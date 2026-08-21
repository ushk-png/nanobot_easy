#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export NANOBOT_PID_FILE="${NANOBOT_PID_FILE:-$SCRIPT_DIR/.local/run/japanese-sentence-coach-gateway.pid}"
export NANOBOT_LOG_FILE="${NANOBOT_LOG_FILE:-$SCRIPT_DIR/.local/logs/japanese-sentence-coach-gateway.log}"

exec "$SCRIPT_DIR/stop-nanobot-skill.sh"
